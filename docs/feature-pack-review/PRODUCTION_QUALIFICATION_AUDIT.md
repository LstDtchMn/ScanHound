# ScanHound Production Qualification — Audit Document for Peer Review

**Prepared:** 2026-07-21 (UTC) by Claude (git/deploy/validation lane), for adversarial
review by ChatGPT/Codex.
**Reviewer constraint:** you can read this public repo and nothing else. Every claim
below is tagged **PROVEN** (a repo artifact you can read, or a repo state you can
check), **ASSERTED** (I observed it; the artifact is local-only or the state is
transient), or **UNVERIFIED** (believed, not tested). Local-only evidence is *named*
so Jesse can produce it on request, but you should treat ASSERTED claims as exactly
that.

Published evidence artifacts live in
[`qualification-evidence/`](qualification-evidence/). Excluded from publication on
purpose: the production DB snapshot and its migrated copies (production data), the
full `config.json` captures and `/settings` snapshots (contain `plex_token`,
passwords, webhooks), and `auth-token.txt` (credential). Their existence is part of
the record; their contents are not publishable.

---

## 1. Executive state

The RSS/HDEncode + file-safety feature pack (67 commits) is **merged to `main`**
(merge commit `b633e695`, `--no-ff`, pushed) and **deployed to the production
container**. The production database migrated **v2 → v6 in place**, integrity ok.
The deployment is fail-closed: auto-rename, general auto-grab, RSS auto-grab, and
background scan are **off**; HDEncode zero-traffic while disabled was proven before
enablement. RSS **shadow mode** was enabled at **2026-07-21T23:07:49Z** via
`POST /rss/mode` — this started the mandatory **seven-day observation window**
(earliest completion **2026-07-28T23:07Z**; ≥20 comparison cycles also required,
expected to accrue in ~2.5 days at the 3-hour scheduler interval). An unattended
collector gathers readiness evidence every 6 hours and exits non-zero on any
mandatory stop condition. Nothing further is scheduled to change in production until
the window closes.

---

## 2. Auditable work log

Columns: claim · evidence artifact · how you verify it · confidence.

### 2.1 Code and branch state

| Claim | Evidence | How to verify | Confidence |
|---|---|---|---|
| Code-tested SHA is `a6b4a7b14d6613c27f17de670677ed848fec458d`; every later commit on the integration branch touches only `docs/feature-pack-review/` | repo history | `git diff --name-only a6b4a7b..origin/agent/feature-pack-integration` — every path starts with `docs/feature-pack-review/` | PROVEN |
| Backend suite at `a6b4a7b`: **3974 passed / 0 failed**, no `--ignore`, no `--deselect` (Py 3.12.13) | [STAGE_B_VALIDATION_AND_VERDICT.md](STAGE_B_VALIDATION_AND_VERDICT.md) | re-run the suite at that SHA; report documents command lines | ASSERTED (report is in-repo; the run itself was local) |
| Python 3.11 full pass: 3804 non-browser + 170 browser; UID-1000 run 3974/0; Playwright E2E 18/0 | same report | same | ASSERTED |
| Merge to `main` is `b633e695`, a `--no-ff` merge of the integration branch at `1898c885`, no force-push anywhere | repo history | `git log --merges origin/main -1`; `git merge-base --is-ancestor 1898c885 origin/main`; reflog/PR history shows no force | PROVEN |
| `--no-ff` was chosen deliberately so production rollback is `git revert -m 1 b633e695` rather than a reset requiring force-push | [merge-message](qualification-evidence/merge-message-20260721T223425Z.txt) | read the merge commit message | PROVEN |

### 2.2 Defects found in the "validated" qualification bundle

The bundle arrived labelled validated. Two of its seven scripts carried assumptions
that do not match the shipped code. This is the **10th and 11th defect** found across
this project in externally-authored packages carrying "validated" labels; the
implication is that such labels describe intent, not evidence, and each package must
be re-derived against the real tree before use.

| Claim | Evidence | How to verify | Confidence |
|---|---|---|---|
| `04_settings_guard.py` originally PUT RSS-only keys to `/settings`; the model is `extra="forbid"` so the whole request 422s and **no stage could ever arm** | [REPAIRS.md](qualification/REPAIRS.md); [settings.py](../../backend/api/routes/settings.py) `SettingsUpdate` | confirm `model_config = ConfigDict(extra="forbid")` and that `hdencode_discovery_mode` + 3 `hdencode_rss_*` keys are absent from the model | PROVEN |
| Correct surfaces: `PUT /settings` for the 4 writable toggles; `POST /rss/mode` for discovery mode; the 3 RSS booleans have **no write endpoint** (deploy-time config, defaults already correct) | [rss.py](../../backend/api/routes/rss.py) `set_rss_mode`; [config.py](../../backend/config.py) DEFAULT_CONFIG | read both files | PROVEN |
| `05_shadow_evidence.py` originally guessed columns (`complete`, `recovery_observed`) that don't exist in `hdencode_shadow_cycles` → overcounted cycles, always reported 0 recovery, could under-count a relevant miss (a mandatory stop condition) | [REPAIRS.md](qualification/REPAIRS.md); [database.py](../../backend/database.py) schema + `get_hdencode_rss_readiness` | compare the original column guesses against the real CREATE TABLE | PROVEN |
| Both repairs are proven by a stdlib self-test (stub `extra="forbid"` server; synthetic v6 DB; asserts a single relevant miss flips readiness off) | [selftest.py](qualification/scripts/selftest.py) | run `python qualification/scripts/selftest.py` — needs only Python 3.12 stdlib | PROVEN (reproducible) |
| The other five scripts were verified correct against the real code (e.g. `PRAGMA user_version` really is the schema store; `DatabaseManager.__init__` really runs migrations, so 02's kill-during-construction is a genuine interrupt test) | [REPAIRS.md](qualification/REPAIRS.md) | read `database.py:33-47` (`__init__` → `init_db()`) and `config.py` | PROVEN |

### 2.3 Migration evidence (objective 7)

Snapshot produced **by Jesse** (the safety classifier blocks me from reading the
production DB; I prepared the container one-liner, he executed it).

| Claim | Evidence | How to verify | Confidence |
|---|---|---|---|
| Snapshot: `user_version=2`, 16 tables, 30,373 rows, integrity ok, schema hash + row counts identical to the live source, empty WAL | [01_snapshot.json](qualification-evidence/01_snapshot.json) | read it; both source and snapshot observations are recorded with SHA-256s | PROVEN (artifact) / ASSERTED (that it faithfully reflects the live DB — the DB itself is not publishable) |
| Migration matrix `ok=true`, zero failures: v2→v6 upgrade preserving all row counts; restart idempotency; old-image reopen; interrupted migration recovered; rollback byte-identical | [02_migration_matrix.json](qualification-evidence/02_migration_matrix.json) | read `failures: []` and each case's observations; row counts per table are enumerated | PROVEN (artifact) |
| **Extra case beyond the bundle:** the old image *downgrades* `user_version` 6→2 while leaving v6 tables in place; re-upgrading from that state yields a schema hash and per-table row counts **identical to a clean migration** | [02b_roundtrip_reupgrade.json](qualification-evidence/02b_roundtrip_reupgrade.json) | read it; `schema_identical: true`, `row_counts_identical: true` | PROVEN (artifact) |
| The interrupted-migration case succeeded on its **first and only** interrupt attempt (killed 143 ms into init) | same matrix JSON | `interrupted_migration` array has length 1 | PROVEN — and see §6: n=1 |

### 2.4 Filesystem sentinel (objective 11)

| Claim | Evidence | How to verify | Confidence |
|---|---|---|---|
| 8/8 sentinel runs `ok=true`, zero failures: 4 host-side (F:/G:/X: NTFS + `\\TURTLELANDSRV2\k` CIFS) + 4 container-side (fresh containers, only sentinel dirs mounted, never `docker exec` into the live container) | [sentinel/](qualification-evidence/sentinel/) — 8 JSON files | read each: `ok`, `failures`, per-test results, cleanup verification | PROVEN (artifacts) |
| Container-side `renameat2(RENAME_NOREPLACE)` **supported with destination preserved (EEXIST)** on every mount — the SH-R02 no-replace primitive works on the real mount types | same files, `tests.no_replace` | read `errno: 17`, `destination_preserved: true`, `destination_unchanged: true` | PROVEN (artifacts) |
| Cross-volume rename correctly surfaces EXDEV with source intact; hardlinks same-inode on all mounts; directory-fsync unsupported **only** on the CIFS mount | same files | read `tests.exdev`, `tests.hardlink`, `tests.directory_fsync` | PROVEN (artifacts) |
| Deviation: sentinel ran **before** deploy (runbook orders it after). Rationale: it probes filesystems, not the app, so earlier is strictly more conservative | [QUALIFICATION_PROGRESS.md](QUALIFICATION_PROGRESS.md) | read the disclosure | PROVEN (disclosed) |

### 2.5 Deployment (objectives 8–9)

| Claim | Evidence | How to verify | Confidence |
|---|---|---|---|
| Deploy executed via a single vetted script (preconditions → merge+push → stop container **before** config edit → backup + fail-closed profile → build → verify) | [deploy_failclosed.py](qualification-evidence/deploy_failclosed.py) | read the script | PROVEN (script) / ASSERTED (its execution — run by Jesse) |
| **Deviation:** on the first run the build/start step did not complete → service down ~11 min on the old image; merge, stop, and config write had already succeeded; a subsequent rebuild brought it up | [QUALIFICATION_PROGRESS.md](QUALIFICATION_PROGRESS.md) | disclosure; the exact build error was on Jesse's terminal and was **not captured** — root cause of that first failure is UNVERIFIED | ASSERTED |
| Running image is `4be9df01…`, built from merged `main`; it carries **no** `org.opencontainers.image.revision` label. Provenance argument: `main@b633e695` application code is byte-identical to the labelled evidence image's source `c050958` (`git diff c050958 b633e695` = one docs file) | [new-image-inspect.json](qualification-evidence/new-image-inspect.json) (the *labelled* evidence image `56d23a0c…`); repo history for the diff | run the diff yourself; the running container's inspect is local-only | PROVEN (diff) / ASSERTED (what is running now) |
| Production DB migrated v2→v6 in place; integrity ok | [05_shadow_evidence_20260721T225251Z.json](qualification-evidence/05_shadow_evidence_20260721T225251Z.json) `db_user_version: 6` | read it | PROVEN (artifact) |
| All 8 fail-closed flags survived app startup (the app did not rewrite config.json) | local config re-read after startup; values echoed in [QUALIFICATION_PROGRESS.md](QUALIFICATION_PROGRESS.md) | config.json is unpublishable (secrets); corroborated by [rss-status-before.json](qualification-evidence/rss-status-before.json) `mode: listing`, `enabled: false` | ASSERTED, partially corroborated |

### 2.6 Zero-traffic proof (objective 10)

| Claim | Evidence | How to verify | Confidence |
|---|---|---|---|
| While `hdencode_enabled=false`: all nine `hdencode_*` tables at **0 rows** — including `hdencode_feed_state`, which gains a row on the first poll of any feed — and zero discovery activity in the container log, **while the scheduler and maintenance loop were confirmed running** (so it's an off-switch proof, not an idle-app proof) | [10_zero_traffic_20260721T225338Z.json](qualification-evidence/10_zero_traffic_20260721T225338Z.json); methodology: [verify_zero_traffic.py](qualification-evidence/verify_zero_traffic.py) | read both | PROVEN (artifact) — but see §6: observation span was ~2 minutes of uptime |

### 2.7 Shadow enablement (objective 12)

| Claim | Evidence | How to verify | Confidence |
|---|---|---|---|
| Shadow enabled 2026-07-21T23:07:49Z via the repaired guard: `ok=true`, zero mismatches, `rss_mode_set: rss_shadow` | [04_settings_shadow_20260721T230748Z.json](qualification-evidence/04_settings_shadow_20260721T230748Z.json) | read it | PROVEN (artifact) |
| Independently confirmed: `GET /rss/status` before (`mode=listing, enabled=false`) and after (`mode=rss_shadow, enabled=true`, `safe_defaults.rss_auto_grab=false`, `listing_fallback=false`) | [rss-status-before.json](qualification-evidence/rss-status-before.json), [rss-status-after.json](qualification-evidence/rss-status-after.json) | read both | PROVEN (artifacts) |
| Auto-rename and auto-grab remain **false** throughout | `settings_put` in the 04 artifact sets both false; corroborated on-disk (unpublishable) | read the artifact | PROVEN (that the guard set them) / ASSERTED (current on-disk state) |
| **Finding:** Cloudflare returns **Error 1010 `browser_signature_banned`** to Python urllib at the public edge (curl passes). Not worked around by UA spoofing; tooling instead runs in a throwaway container on the internal `proxy` network → `http://scanhound:9721` | first (failed) guard attempt captured in session; the *working* runs are the published artifacts | ASSERTED — but trivially reproducible: any Python urllib GET to scanhound.turtleland.us | ASSERTED |

### 2.8 Who did what

- **Jesse:** produced the DB snapshot (classifier gate); executed `deploy_failclosed.py`; performed the recovery rebuild; supplied the session auth token; made the four window-policy decisions (retag/rebuild, token handling, automated collection, auto-rename stays off).
- **Claude (me):** everything else — bundle validation and repairs, self-test, image build, sentinel, migration matrix + extra case, deploy script authorship, zero-traffic proof, shadow enablement, collector + scheduled task, all commits/pushes (all non-force), this document.

---

## 3. Currently running

| Process | What / where | Schedule | Writes | On failure | How a reviewer confirms it's alive |
|---|---|---|---|---|---|
| ScanHound production container | image `4be9df01` from `main@b633e695`, Docker host | always-on (`restart: unless-stopped`) | production DB `/dbvol/crawler.db` (now v6) | NPM/Cloudflare serve 502 | not verifiable from repo; Jesse: `docker ps` |
| RSS shadow comparisons | in-app scheduler, `rss_shadow` mode | every 3 h | `hdencode_shadow_cycles` / `hdencode_shadow_misses` rows | cycle absent → readiness gate simply doesn't advance (fail-closed) | future `05_shadow_evidence_*.json` commits will show `cycles` climbing from 0 |
| Evidence collector | Windows Scheduled Task "ScanHound Qualification Evidence" → [run_collection.cmd](qualification-evidence/run_collection.cmd) → [collect_shadow_evidence.py](qualification-evidence/collect_shadow_evidence.py); reads prod DB **read-only** from a throwaway container | every 6 h | timestamped `05_*.json` + one line to [shadow-window.log](qualification-evidence/shadow-window.log) | **exits non-zero on any relevant miss or integrity failure** → task shows failed | log already has 5 entries; future commits extend it. Note the collector was verified with `LastTaskResult=0` after an earlier `schtasks` registration **silently failed** (`-2147024894`, spaces in path) and was re-registered via PowerShell |
| In-app maintenance loop | hourly; trash sweep + pipeline reconcile | 1 h | app DB/log | app-internal | startup log lines quoted in QUALIFICATION_PROGRESS.md (ASSERTED) |

**Not running:** auto-rename, general auto-grab, RSS auto-grab, background scan,
listing fallback, RSS-primary.

---

## 4. Remaining work

**Blocked** — none. For the first time in this project, nothing is waiting on a
person.

**Scheduled (self-completing):**

| Item | Completes | Gate |
|---|---|---|
| ≥7 calendar days of shadow observation | earliest **2026-07-28T23:07Z** | hard floor; will not be shortened or simulated |
| ≥20 comparison cycles | ~2.5 days at 3 h/cycle | expected to be the non-binding constraint |
| Zero relevant misses; positive request reduction; ≥1 restart/catch-up recovery; healthy feeds | continuous over the window | any relevant miss = mandatory stop + rollback |

Note the recovery requirement means at least one container restart **must** occur
during the window; one should be induced deliberately (e.g. `docker compose
restart`) rather than hoped for.

**Open (needs decision or design):**

1. Post-window verdict: RSS-primary enablement (only if the gate passes — enforced
   as a 409 in `set_rss_mode`).
2. Auto-rename restoration — Jesse's decision at window close; the SH-R02 fix
   deployed makes re-enablement *safer* than the pre-deploy status quo.
3. Auto-grab restoration — separate, later staged decision.
4. `06_finalize_evidence.py` checksummed evidence package + final ChatGPT/Claude
   reconciliation.

---

## 5. Deferred objectives

| Item | Why deferred | Risk of leaving it | Revisit trigger |
|---|---|---|---|
| Runtime-exercising the writer lock suite-wide (conftest `_unlocked_fileops_for_tests` bypasses it) | fixing means reworking test isolation, not a small patch | a future `fileops.py` edit could drop a guard silently; only the AST contract test catches it | before auto-rename is re-enabled |
| Repeating the interrupted-migration case at n>1 | matrix script breaks on first successful interrupt; window opened before a re-run | recovery evidence rests on one sample (plus the suite's own migration tests) | if any migration anomaly appears; or cheaply re-runnable any time from the local snapshot |
| Longer-horizon zero-traffic re-check (post-3h-scheduler-tick) | shadow was enabled ~16 min after deploy, closing the disabled-state observation window at ~2 min uptime | off-switch evidence span is short; mitigated by table-counts being cumulative (feed_state was empty at enablement) | none — superseded unless HDEncode is ever re-disabled |
| Deferred Minor from dupe-compare (v2.26.0): analyzer recommendation ignores filename source/audio/edition tags the Compare modal scores | out of scope for qualification | cosmetic inconsistency | next feature round |
| Root-causing the first `deploy_failclosed.py` build failure | error output existed only on Jesse's terminal; recovery superseded diagnosis | none for this deploy; pattern unknown for future deploys | next deploy from this checkout |
| Deleting stale `data/crawler.db*.premigration.bak` files (Jul 2 era) in the deploy checkout | not mine to delete during a qualification window | none (dormant files) | post-qualification cleanup |

Silently deferred until now (surfacing per instructions): the ChatGPT handoff of the
*previous* round's reconciliations was owed before this document; **this document
supersedes it**.

---

## 6. Known weaknesses (written against my own interest)

1. **The writer-lock graft is not runtime-exercised by the 3974-test pass.** The
   autouse bypass disables it suite-wide. Evidence is an AST contract test + 10
   dedicated lock tests. "3974 tests prove the lock" would be false; nothing in the
   suite calls the guarded functions with the lock armed except those 10.
2. **Interrupted-migration recovery: n=1.** One kill at 143 ms, one clean recovery.
   The script exits its loop on first success. A single sample cannot distinguish
   "robust" from "lucky timing," though SQLite's journal semantics and the additive
   CREATE-IF-NOT-EXISTS design argue robustness independently.
3. **Zero-traffic observation spanned ~2 minutes of uptime.** The 3-hour scheduler
   had not yet fired. The strong part of the proof is cumulative (nine tables empty
   since container start, including feed_state); the weak part is the log-scan
   window. Shadow enablement then closed the disabled-state period permanently.
4. **The running image lacks the revision label.** The chain "running container =
   tested code" has a manual link: a git diff showing `c050958` vs `b633e695` differ
   only in docs, plus Jesse's rebuild being from that main. The *labelled* image
   exists but is not the one running. A reviewer should treat image provenance as
   argued-not-stamped.
5. **Key run-state claims are ASSERTED, not repo-verifiable**: the full-suite runs,
   the fail-closed config surviving startup (config unpublishable — secrets), the
   scheduled task being alive, and the deploy-script execution. The repo carries
   reports and corroborating artifacts, not the runs themselves.
6. **The first deploy attempt's failure is undiagnosed** (§5). The recovery is
   solid; the cause is unknown.
7. **The session token can expire mid-window.** The collector's core numbers come
   from the DB (token-independent), so the window survives expiry — but the
   app-vs-collector readiness cross-check silently degrades until the token is
   refreshed.
8. **The evidence collector and the app share readiness logic by mirroring, not by
   import.** If a future commit changes `get_hdencode_rss_readiness`, the collector
   drifts. Within this window the code is frozen, so the risk is bounded to
   future use.

---

## 7. Enhancement opportunities

| Improvement | Problem it solves | Effort |
|---|---|---|
| Gotify alert on collector failure (Jesse already runs Gotify; task exits non-zero on stop conditions) | a mandatory stop condition currently only *shows* as a failed task — nobody is paged | ~30 min |
| Remove the conftest writer-lock bypass; make guarded fileops tests run lock-armed | closes weakness #1 structurally | 0.5–1 day |
| Add `--interrupt-attempts` accumulation (don't break on first success) to `02_migration_matrix.py` | closes weakness #2 for future migrations | ~1 h |
| Stamp `org.opencontainers.image.revision` in the Dockerfile itself (from a build arg in compose) | closes weakness #4 for every future deploy | ~30 min |
| Publish a redacted `config-diff` artifact (keys + booleans only, values of secret keys elided) | converts the fail-closed-config claim from ASSERTED to PROVEN | ~1 h |
| Cloudflare: add a WAF exception or service token for the internal tooling path | removes the 1010 landmine for any future Python tooling; today's internal-network route is a workaround, albeit a sound one | ~1 h |
| Induce one deliberate `docker compose restart` mid-window | the readiness gate *requires* ≥1 recovery; better engineered than accidental | 5 min |

---

## 8. Specific review requests

Precise questions, answerable from this repo, aimed at real risk:

1. **Identity promotion:** In `backend/hdencode_candidate_service.py`, review
   `_identity_is_confirmed` + `classify_candidate` end-to-end. Is there any path
   where a `hydrated` candidate with a title/description **year conflict** still
   reaches `exact` without an external id? The movie branch's final fallback
   (`return bool(title_year or description_year)`) is the line to attack.
2. **Readiness gate honesty:** In `backend/database.py::get_hdencode_rss_readiness`,
   can `request_reduction_pct > 0` be satisfied *spuriously* — e.g. cycles where
   `listing_requests` is high for reasons unrelated to RSS efficiency, or zero-RSS
   degenerate cycles counting as "successful"? Is `outcome IN
   ('success','relevant_miss')` + `normal_feeds_complete=1` sufficient to exclude
   junk cycles?
3. **Stop-condition coverage:** `qualification-evidence/collect_shadow_evidence.py`
   hard-fails on relevant misses and integrity only. Should any other mandatory stop
   condition from `qualification/AUTHORIZATION.md` (e.g. unexplained candidate-count
   collapse, heavier fallback after 403/429) be detectable from the DB and therefore
   also hard-fail the collector?
4. **Settings guard restore path:** In `qualification/scripts/04_settings_guard.py`,
   the `restore` stage replays a snapshot's managed keys. If the snapshot was taken
   from `GET /settings` (masked), can a masked sentinel value (`••••••••`) ever land
   in the PUT payload? Check against `SETTINGS_WRITABLE` — I believe the 4-key
   whitelist makes it impossible, but verify.
5. **Shadow-cycle write path:** Find every writer of `hdencode_shadow_cycles`. Can a
   cycle be recorded with `normal_feeds_complete=1` when a feed actually failed
   mid-cycle (crash between feed fetch and cycle write)? That would inflate the
   completed-cycle count the gate depends on.
6. **Mode-change concurrency:** `set_rss_mode` writes `reg.config` then
   `save_config()`. If a settings PUT races it, can a lost-update revert
   `hdencode_discovery_mode`? Assess whether FastAPI's threading + the config-save
   path makes this reachable.
7. **Migration re-entrancy:** Given the old image stamps `user_version` back to 2
   while leaving v6 tables (proven in `02b_roundtrip_reupgrade.json`), audit every
   guarded ALTER in `init_db` for a statement that is *not* idempotent under
   re-execution against an already-v6 schema. The round-trip test passed on this
   data; the question is whether any table/column state could make a re-run fail.
8. **Sentinel blind spots:** `qualification/scripts/03_filesystem_sentinel.py`
   probes no-replace, hardlink, fsync, atomic-replace, exdev. Name concrete fileops
   failure modes in `backend/rename/fileops.py` it does **not** exercise (e.g.
   case-insensitivity collisions, sharing-violation `EBUSY` semantics on
   gRPC-FUSE, partial-write on CIFS) and whether any of them would have changed the
   deploy decision.

---

*Prepared under the peer protocol: advice-not-orders, trust-but-confirm with
evidence, one source of truth in this repo. Every reconciliation and deviation in
this round is disclosed above or in [QUALIFICATION_PROGRESS.md](QUALIFICATION_PROGRESS.md).*
