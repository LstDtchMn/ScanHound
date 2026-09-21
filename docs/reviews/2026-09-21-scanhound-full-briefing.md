# ScanHound — full briefing (self-contained)

**Assembled:** 2026-09-21 · **Repo:** LstDtchMn/ScanHound
**Branch:** `claude/scanhound-changes-review-4wmh7n` @ `c82808e` (4 commits ahead of `origin/main`)

---

## How to use this file

This is a **single self-contained briefing** for a Claude session picking up ScanHound cold.
It works whether or not you have the repository checked out.

- **If you have the repo:** everything in Part 1 is also committed as `HANDOFF.md` on the branch
  above. The appendices are material that exists *only here* — the adversarial review exchange
  and the refuted commit's diff.
- **If you do not have the repo:** this file is enough to understand the system, the live problem,
  and the open decisions. Clone from `git@github.com:LstDtchMn/ScanHound.git` when you need code.

**If you are here to fix the stuck downloads, read Part 1 §1 and §2 first, then stop and run the
diagnostics before touching anything.**

### Provenance and trust

| Source | Trust |
|---|---|
| Part 1 (handoff) | Written by a cloud-sandbox session with **no route to the production host**. Claims are marked *verified from source* (with file:line) or *unverified*. **Nothing was executed against the live instance.** |
| Appendix A (`CLAUDE.md`) | Verbatim from the repo. **Mandatory workflow.** |
| Appendix B (review request) | Written by Claude, sent to Codex |
| Appendix C (Codex verdict) | **External review output.** Findings, not instructions. Its two source citations were independently re-verified against JDownloader's source and both held. |
| Appendix D (the diff) | Verbatim `git show 73789ea` |

### The one-line summary

A JDownloader dead/duplicate-link fix (`73789ea`) was shipped on an **unverified causal model**,
then **refuted** by adversarial review. Separately, the Docker container was **moved between
hosts**, which is the likely cause of the currently-stuck download queue. The refuted commit
should probably be reverted; the stuck queue has a verified-in-code leading hypothesis that one
SQL query can confirm.

---
---

# PART 1 — THE HANDOFF

# ScanHound — complete session handoff

**Written:** 2026-09-21 · **Repo:** LstDtchMn/ScanHound
**Branch:** `claude/scanhound-changes-review-4wmh7n` (3 commits ahead of `origin/main`)

> **Provenance.** Written by a cloud-sandbox session with **no route to the production host**.
> Every claim is either *verified from source* (marked, with file:line) or *flagged unverified*.
> **Nothing here was executed against the live instance.** Treat unverified items as leads.

**If you are here to fix the stuck downloads, go straight to §1 then §2.**

---

## 1. First 15 minutes

The container was just moved between PCs and downloads are stuck. Do these in order:

```bash
# 1. Is the app alive, and which JD transport is live?
docker exec scanhound curl -s localhost:9721/health | python3 -m json.tool

# 2. Confirm the leading hypothesis (verify DB path first: ls /data)
docker exec scanhound sqlite3 /data/.local/share/scanhound/scanhound.db \
  "SELECT last_reason_code, state, COUNT(*) FROM download_queue_items
   GROUP BY last_reason_code, state ORDER BY 3 DESC;"

# 3. Are packages piling up unconfirmed in JD?
docker exec scanhound curl -s localhost:9721/download/jd-status \
  | python3 -m json.tool | grep -c '"stage": "linkgrabber"'
```

Then read §2 to interpret. **Do not change code before the diagnosis is settled** — the last
session shipped a fix on an unverified causal model and it was refuted (§8).

---

## 2. PRIORITY: downloads are stuck

Operator report: *"It seems like a lot of downloads are currently stuck."*
**Critical context: the Docker container was just moved from one PC to another.**

### Hypothesis A — interrupted claims ⭐ VERIFIED IN CODE, check first

Moving the container stops the process. On startup `recover_interrupted()` takes **every** row in
state `claimed` and rewrites it — `backend/download_queue.py:276-295`:

```sql
UPDATE download_queue_items
SET state = 'failed', queue_reason = 'manual_retry',
    last_reason_code = 'interrupted_unknown_outcome',
    transport_attempted = 1, ...
WHERE state = 'claimed'
```

Those rows are then **permanently excluded from automatic claiming** — verified at three sites:
`_claim_due()` at `download_queue.py:752-755` and `863-866`, auto-resume at `1935-1938`:

```sql
AND COALESCE(last_reason_code, '') NOT IN (
    'operation_timeout_unknown', 'interrupted_unknown_outcome')
```

**Deliberate, not a bug**: an interrupted item's delivery outcome is unknown and auto-retrying
risks double-submitting a grab that already reached JDownloader. But a host move therefore strands
every in-flight item until a human acts. **This fits the symptom best.**

### Hypothesis B — JD dupe/offline `ASK` dialog — VERIFIED against JDownloader source (§8)

If JD's LinkGrabber is set to `ASK` on added dupes or offline links it opens a **modal dialog**,
blocking JD's GUI confirm path but **not** its API. Signature: `jd_poll` reads healthy while
packages pile up at `stage: "linkgrabber"` and the Downloads list stays static.

### Hypothesis C — stale `jd_device` after the move — UNVERIFIED

`_connect_jd_device()` calls `jd.get_device(device_name)` from config `jd_device` and **raises** if
the name is not found. If JD was reinstalled/renamed on the new PC, every send and poll fails.
Signature: `jd_poll.failure_phase == "get_device"`.

### Hypothesis D — host-dependent config broken by the move — UNVERIFIED

`docker-compose.yml` maps `host.docker.internal:host-gateway` for Plex `:32400` and joins an
**external** `proxy` network. Verify: the `proxy` network exists on the new host, Plex is reachable,
`./data` came across with the SQLite DB intact.

### Hypothesis E — Plex repoint — CROSS-SESSION (§13)

A parallel session *"Plex library repoint adversarial review"* was active 2026-09-21 01:20 —
*"six hypotheses eliminated; traps set for event-centric correlation."* Overlaps the migration.
**Check it before duplicating that investigation.**

---

## 3. Diagnostic playbook

`/health` is **deliberately unauthenticated** so a host watchdog can read it without a credential
(`backend/api/routes/system.py:17`). Everything else needs auth.

| `/health` field | Meaning |
|---|---|
| `jd_enabled` | `jd_enabled AND jd_method == "api"` — **resolves which transport is live** |
| `queue.executor_starved` | Work due, nothing **started** — scheduler/ownership/liveness fault |
| `queue.source_no_progress` | Attempts running, source delivering nothing — source fault |
| `queue.human_required` | Verification hold or auto-resume exhausted — **no timer clears this** |
| `jd_poll.failure_phase` | `connect` / `update_devices` / `get_device` / `query_packages` |
| `jd_poll.stalled_seconds` | Seconds since JD last answered |

Three stall conditions are separated deliberately: one timer cannot tell *"nothing was attempted"*
from *"everything attempted failed"* (`database.py:6069`).

`jd_poll` liveness triage (`download_service.jd_poll_health`):

| Signature | Diagnosis |
|---|---|
| `cycles_started == cycles_completed`, static | Poller thread **stopped** |
| `cycles_started > cycles_completed`, `current_cycle_seconds` growing | **Blocked** mid-call |
| Both advancing, `stalled_seconds` growing | Cycling but **failing** — read `failure_phase` |

---

## 4. Remediation

**Hypothesis A.** Both retry paths explicitly clear the blocking reason code
(`download_queue.py:2286-2291`, `2366-2372`) — `last_reason_code = NULL`, state `ready`:

- `POST /download/retries/{item_uuid}/retry` — one item
- `POST /download/retries/retry-ready` — bulk, takes an interval

> ⚠️ **Check JDownloader for already-delivered packages before bulk-retrying.** The exclusion
> exists precisely because these outcomes are unknown. A blind bulk retry can duplicate grabs.

**Hypothesis B.** JDownloader → **Settings → LinkGrabber** → set added-dupes and added-offline
actions to `EXCLUDE` / `EXCLUDE_OFFLINE` (*skip, leave in the Links menu*).
**Never** the `_AND_REMOVE` variants — those delete the links.

**If `human_required`:** `GET /download/retries` lists held items;
`POST /download/verification-hold/clear` is the operator escape hatch.

---

## 5. Architecture map

**Stack:** FastAPI backend + Svelte 5 frontend, one origin, one port (**9721**), SQLite, Docker.
A Tauri v2 Android app shares the frontend.

### Backend — `backend/`

| Area | Modules |
|---|---|
| **API** | `api/main.py` (app + auth middleware), `api/dependencies.py` (`ServiceRegistry`, `auth_enabled`, `token_authorized`), `api/ws.py` (WebSocket), `api/routes/*.py` |
| **Scanning** | `scanner_service.py`, `background_scanner.py`, `link_scraper.py`, `detail_scraper.py`, `metadata_enricher.py` |
| **Sources** | `sources/` — `hdencode*.py`, `ddlbase.py`, `adithd.py`, `registry.py`, `base.py`; identity unified in `source_identity.py` |
| **Downloads** | `download_service.py` (JD transport, scraping, poller — **~4200 lines, the big one**), `download_queue.py` (durable queue + scheduler), `download_outcome.py`, `queue_recovery_policy.py`, `clicknload.py` |
| **Rename** | `rename/` — `service.py`, `naming.py`, `fileops.py`, `confidence.py`, `llm_identify.py`, `dv_detect.py`, `conflicts.py`, `episodes.py` |
| **Plex** | `plex_manager.py`, `plex_service.py`, `plex_metadata_scan.py` |
| **Data** | `database.py` (**very large**, all schema + queries), `models.py`, `config.py` |
| **Other** | `auth_service.py`, `analytics.py`, `watchlist.py`, `notifications.py`, `auto_grab_service.py`, `pipeline_service.py` |

### API surface (prefixes, `backend/api/routes/`)

`/auth` · `/scan` · `/results` · `/download` · `/rename` · `/plex` · `/settings` · `/sources`
· `/rss` · `/watchlist` · `/analytics` · `/pipeline` · `/scheduler` · `/background`
· *(no prefix)* `/health`, `/discover`, `/shutdown`

### Frontend — `frontend/src/`

Routes: `/` (scan results) · `downloads` · `renames` · `pipeline` · `analytics` ·
`media-inventory` · `rss` · `settings` · `login`.
State in `lib/stores/` (Svelte `writable`/`derived`), components in `lib/components/`,
API client in `lib/api/`. Svelte 5 runes (`$state`/`$derived`).

### Config — the 4-place pattern

Adding a setting touches **four** places, or it silently won't work:
1. `backend/config.py` → `AppConfig` TypedDict
2. `backend/config.py` → defaults dict
3. `backend/api/routes/settings.py` → `SettingsUpdate` (Pydantic, `extra="forbid"`)
4. The Settings UI tab in the frontend

---

## 6. The download queue state machine

`download_queue_items.state` (schema `database.py:1379`):

```
scheduled → claimed → completed
         ↘         ↘ failed
   ready              waiting_source
                      verification_required
                      cancelled
```

| State | Meaning |
|---|---|
| `scheduled` | Waiting for its due time |
| `ready` | Due, promoted (e.g. by a manual retry or item-local deferral) |
| `claimed` | A worker owns it, under a lease. **Interrupted claims become `failed` on restart — §2A** |
| `waiting_source` | Parked by a source-wide pause/cooldown |
| `verification_required` | Behind an interactive challenge — **only a human probe releases it** |
| `completed` / `failed` / `cancelled` | Terminal |

`queue_reason` (`database.py:1375`): `user_batch` · `interactive_challenge` · `source_deferred` ·
`manual_retry`

`download_queue_batches.state` (`database.py:1288`): `scheduled` · `running` · `paused_source` ·
`waiting_user` · `completed` · `cancelled`

**Key invariants** (each learned from an incident — see the code comments, they are unusually good):
- Source pacing is **global per source**, not per batch (capacity vs demand).
- A verification hold is **source-wide** and a timer never releases it.
- Auto-resume has a **budget**; exhausted batches stop and require manual action.
- `operation_timeout_unknown` / `interrupted_unknown_outcome` are **never auto-retried**.
- Ownership predicates (`WHERE ... AND claimed_by = ?`) guard every terminal write against a
  stale worker overwriting a newer one.

---

## 7. Running it

```bash
# Production (the only supported deploy)
docker compose up -d --build

# Backend dev
python -m backend.api --port 9721

# Frontend dev
cd frontend && npm install && npm run dev

# Tests
python -m pytest tests/ -q              # full suite ≈ 11 min, 5457 tests
cd frontend && npm run test:unit        # vitest
cd frontend && npm run check            # svelte-check
cd frontend && npm run test:e2e         # playwright
```

**Test deps are hand-curated in `.github/workflows/tests.yml`, NOT all in `requirements.txt`.**
A working venv needs at least:
```
pytest pytest-asyncio requests aiohttp cloudscraper beautifulsoup4 thefuzz rapidfuzz
python-dotenv packaging selenium PlexAPI fastapi uvicorn httpx bcrypt websockets plyer myjdapi
```

**Container paths:** `HOME=/data`, so config is `/data/.config/scanhound` and data (SQLite DB,
Plex cache, logs) is `/data/.local/share/scanhound`. Host mount is `./data`.

**Networking:** no host port is published. The app is reachable only via the external `proxy`
network as `http://scanhound:9721`, fronted by Nginx Proxy Manager + Cloudflare Tunnel.
**There is no built-in auth in the container** — Cloudflare Access / NPM Access Lists are the
authentication layer.

---

## 8. The JD dead/duplicate-link investigation (closed — commit refuted)

### `73789ea` — "stop dead and duplicate links holding up the download queue"

Pushed, **no PR opened, diagnosis refuted. Recommend reverting.**

Added two best-effort guards to the `api` transport in `send_to_jdownloader`:
`_filter_known_links()` and `_release_online_links()`. 21 new tests; full suite green.

**Claimed premise:** `add_links(autostart=True)` hands the whole package — including OFFLINE
children — to auto-confirm, so one dead mirror holds the package.

**Codex rejected it.** Both claims were then **independently verified against JDownloader source**
(`github.com/mirror/jdownloader`, master):

| ID | Finding | Evidence | Status |
|---|---|---|---|
| **M1** | JD already excludes offline children | `AutoStartManager.java`: `if (child.getLinkState() == AvailableLinkState.OFFLINE) { createNewSelection = true; continue; }` | **CONFIRMED** — the release pass reimplements what JD does natively |
| **M2** | Dupe test is against the *download list*, not the LinkGrabber | `ConfirmLinksContextAction.java`: `} else if (!DownloadController.getInstance().hasDownloadLinkByID(id)) { continue; }` | **CONFIRMED, worse than stated** — the filter was narrowed to LinkGrabber-only mid-session, hitting exactly the wrong population |
| **M3** | Affected transport never established; repo default is `folder` | `config.py:474`; guards sit under `elif jd_method == "api":` | **OPEN** — resolve via `/health` `jd_enabled` |

### The real mechanism (found while verifying M1/M2)

`ConfirmLinksContextAction` reads two configurable settings:
- `CFG_LINKGRABBER.CFG.getDefaultOnAddedDupesLinksAction()` → `INCLUDE | EXCLUDE | EXCLUDE_AND_REMOVE | ASK | GLOBAL`
- `CFG_LINKGRABBER.CFG.getDefaultOnAddedOfflineLinksAction()` → `INCLUDE_OFFLINE | EXCLUDE_OFFLINE | EXCLUDE_OFFLINE_AND_REMOVE | ASK | GLOBAL`

`ASK` opens a modal dialog. **The fix is JD configuration, not ScanHound code** — and it is
transport-independent, which also dissolves M3.

Also verified: **`myjdapi` 1.1.10 `AddLinksQuery` has no `autoConfirm` field** (only `autostart,
links, packageName, extractPassword, priority, downloadPassword, destinationFolder,
overwritePackagizerRules`). An earlier proposed fix adding `autoConfirm` to the API payload was
discarded — it would have been a silent no-op.

**Follow-up not implemented:** have ScanHound *detect* `ASK` on connect and warn, rather than
silently overriding operator JD preferences.

---

## 9. Repo & branch state

```
0b45d6a  docs: expand session handoff with cross-session and repo context
25f8b70  docs: add session handoff for continuing work locally
73789ea  fix(jd): stop dead and duplicate links ...        ← REFUTED, consider revert
0a2751d  Merge pull request #59  (origin/main HEAD, 2026-08-28)
```
Working tree clean · nothing unpushed · 3 ahead / 0 behind `origin/main`.
PR #2 from this branch merged earlier; treat further work as fresh changes.

---

## 10. Read-first document map

| File | What it is | Trust |
|---|---|---|
| `CLAUDE.md` | **Mandatory workflow** — Claude implements, Codex reviews adversarially | Current |
| `CODEX_REVIEW.md` | Codex review protocol in detail | Current |
| `AGENTS.md` | Agent conventions | Current |
| `DOCKER.md`, `DEPLOY_INSTRUCTIONS.md`, `DEVELOPMENT.md` | Deploy / dev setup | Current |
| `docs/TODO.md` | Living remaining-work checklist | 2026-06-29 |
| `docs/reviews/STATE-OF-PLAY.md` | Canonical outstanding work | **STALE — §12** |
| `SCANHOUND-CLOUD-SESSIONS-CATCHUP.md` | Cross-session relay log | Historical |
| `docs/HANDOFF.md`, `docs/HANDOFF-2026-08-17*.md` | Prior handoffs | Historical |
| `docs/runbooks/`, `docs/reviews/`, `docs/designs/`, `docs/specs/` | Deep per-feature history | Varies |

---

## 11. Working agreements & known traps

- **`CLAUDE.md` mandates Codex adversarial review** for non-trivial changes. Ask Codex to
  **challenge**, not confirm. Preserve finding IDs (M1, M2…) across rounds; never silently drop
  one. **Never claim Codex reviewed something unless it actually did.** This session's `73789ea`
  is a live demonstration of why the rule exists.
- **Commit attribution:** end messages with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` plus the `Claude-Session:` line.
- **Deploy only via** `docker compose up -d --build`.
- ⚠️ **`inspect.getsource` trap.** `tests/test_source_progress_contract.py` asserts on the *source
  text* of `download_item`. Editing `backend/download_service.py` **while a suite is running**
  shifts line numbers and yields 5 spurious failures. This happened here and was briefly
  misdiagnosed as a regression. **Never edit source while a suite runs.**
- ⚠️ **Module-global test isolation.** This codebase has repeatedly been bitten by module-level
  globals leaking between test modules (e.g. `_last_scan_items` in `api/routes/scanner.py`). Tests
  can pass isolated and fail only in the full suite. **Always verify with a full run.**
- **Code comments here are unusually valuable.** Many carry incident dates and the reasoning behind
  a non-obvious invariant. Read them before "simplifying" anything in `download_queue.py`,
  `download_service.py` or `database.py`.

---

## 12. Longer-term outstanding work

> **`docs/reviews/STATE-OF-PLAY.md` is STALE.** Dated 2026-08-10, it warns that `main`'s
> `dv_detect.py` can remove a managed Plex label and directs work to
> `agent/dv-detector-consolidation`. That branch **no longer exists on origin**, `main` advanced to
> 2026-08-28 (PR #59, a DV-detector runbook), and `dv_detect.py` has been rewritten (now carries
> `LAYER_FEL`, profile-aware classification, a bounded FEL-positive accelerator, `(MEL, FEL)`
> handling). **The consolidation appears to have landed — re-verify before acting on that warning.**

From `docs/TODO.md` (2026-06-29) — **re-verify all against current `main`**:
- **DV FEL/MEL:** seed importer, real-file accuracy validation, per-file ingest hook, Plex label
  write-path, Kometa overlay, optional `mkvpropedit` file tagging, config wiring.
- **Non-DV:** TV library unset (`auto_rename_tv_library = ""`); `G:\Downloads` not path-mapped as a
  source; `POST /results/select-all` implemented but unwired; live-mode scan pagination deferred
  (>500 results unreachable until a fresh scan).

---

## 13. Related Claude sessions

Titles are session metadata; **contents were not read**. Unverified leads, not fact.

| Session | Relevance |
|---|---|
| **Plex library repoint adversarial review** | **Most relevant.** Active 2026-09-21 01:20, *"investigation complete; awaiting old-server script run"*, *"six hypotheses eliminated"*. Artifacts: "Plex Repoint R21/R22" |
| Turtleone / Turtletwo (Old Main) / turtlelandsrvr Server Work | The server migration itself |
| Docker setup and app container updates (several) | Container/deploy history |
| Turnstile verification hold timer · "Add a verification hold a timer cannot release" | Origin of the queue's verification-hold design — relevant if `human_required` |
| DV scan cannot converge: 30-min timeout + retry starvation | Queue starvation precedent |
| Stream DV detector output live instead of buffering | DV live-progress work |
| Handoff documentation and session setup (several) | Prior handoff conventions |

---

## 14. Open questions

1. Live `jd_method` — `folder` or `api`? (`/health` → `jd_enabled`)
2. How many rows carry `interrupted_unknown_outcome`? (§2A)
3. JD's added-dupes / added-offline actions — is either `ASK`?
4. Did `./data` survive the move with the SQLite DB intact?
5. Keep or revert `73789ea`?
6. Did the Plex repoint work conclude, and does it bear on the stuck queue?

## 15. Do not

- Do not build on `73789ea` — its causal model is refuted.
- Do not bulk-retry interrupted items before checking JD for already-delivered packages.
- Do not use the `_AND_REMOVE` JD actions; dead/dupe links should **stay** in the Links menu.
- Do not act on `docs/reviews/STATE-OF-PLAY.md` §1 without re-verifying — it is stale.
- Do not edit source files while a test suite is running (§11).
- Do not ship a fix on an unverified causal model. That is exactly what `73789ea` did.


---
---

# APPENDIX A — `CLAUDE.md` (mandatory workflow, verbatim)

> Inlined so this file stands alone. This governs how work is done in this repo.

﻿<!-- BEGIN CLAUDE-CODEX-PEER-REVIEW-WORKFLOW -->
# Claude + Codex Peer Review Workflow

Claude Code is the primary implementation agent.

Codex is an independent reviewer.

## Claude responsibilities

Claude should:

- inspect the real repository before changing code
- implement the requested work
- run appropriate tests
- explain meaningful design decisions
- treat Codex findings as independent peer-review findings
- investigate findings rather than blindly accepting or rejecting them
- fix valid findings
- explain rejected findings using repository evidence
- rerun relevant tests after remediation

Claude must not claim that Codex reviewed something unless an actual Codex
review was executed.

## Review cycle

For non-trivial changes, the preferred process is:

IMPLEMENT
    |
    v
RUN TESTS
    |
    v
CODEX ADVERSARIAL REVIEW
    |
    v
CLAUDE INVESTIGATES FINDINGS
    |
    v
REMEDIATE VALID FINDINGS
    |
    v
RUN TESTS
    |
    v
CODEX TARGETED RE-REVIEW
    |
    v
FINAL CODEX REVIEW
    |
    v
READY FOR HUMAN MERGE DECISION

## Handling findings

Claude should preserve the identity of findings across review rounds.

Example:

M1 - OPEN
M2 - CLOSED
M3 - PARTIALLY CLOSED

Claude should not silently drop findings.

## Reviewer independence

Do not ask Codex merely to confirm Claude's conclusions.

Ask Codex to challenge them.

For important changes, Codex should specifically examine:

- hidden assumptions
- failure modes
- concurrency
- data loss
- rollback
- recovery
- transactional boundaries
- regression risk
- test adequacy

Claude remains responsible for implementation decisions after considering the
review.
<!-- END CLAUDE-CODEX-PEER-REVIEW-WORKFLOW -->

---
---

# APPENDIX B — the adversarial review request sent to Codex

> Written by Claude. Included because it records exactly which claims were *assumed* versus
> *verified* at the time — the distinction that the review then turned on.

# Codex adversarial review request — ScanHound `73789ea`

**Repo:** LstDtchMn/ScanHound
**Branch:** `claude/scanhound-changes-review-4wmh7n`
**Commit:** `73789ea` — *fix(jd): stop dead and duplicate links holding up the download queue*
**Base:** level with `origin/main` at time of commit
**Diff:** `backend/download_service.py` (+179/-10), `tests/test_download_service.py` (+225)

Implementation was done by Claude. **Your job is to challenge it, not to confirm it.**
Do not summarise what the change does back to me — I know what it does. Find what is
wrong with it.

---

## 1. The reported defect

Operator report, verbatim:

> "when scan hound adds links to Jdownloader it isn't sending them for download
> automatically or it is getting stuck when duplicates or dead links are found"
>
> "I see the duplicate links and or dead links warning in the jdownloader windows
> application and it holds up the other downloads. Id say by default duplicate links
> and dead links should be skiped and remain in the links only menu"

Desired behaviour: dead + duplicate links are skipped by default and left in the
LinkGrabber ("Links" menu); good links download without waiting on them.

## 2. Diagnosis Claude arrived at (CHALLENGE THIS FIRST)

`send_to_jdownloader`'s API transport calls
`device.linkgrabber.add_links([{ "autostart": True, ... }])`. The claim is that
`autostart=True` hands the **whole package** to JDownloader's auto-confirm, which
**holds** a package containing offline or duplicate children and surfaces the
dead/duplicate warning in the desktop app — so one dead mirror stalls the whole grab.

**This causal claim is the foundation of the entire change and was never verified
against a live JDownloader.** It was inferred from code reading plus the operator's
description. If it is wrong, the fix is ceremony.

Specifically attack:
- Does `autostart=True` actually hold on offline children, or does JD confirm the
  package and simply mark the dead links failed? If the latter, what is actually
  blocking the operator's queue, and does this change address it at all?
- Is the blockage instead JD's **global** "dupe detected" modal (a UI-level dialog
  that blocks the whole LinkGrabber), which no API-side link filtering would clear?
- The crawljob transport already sets `autoConfirm`/`autoStart`/`forcedStart`. The
  operator's `jd_method` was **not confirmed** to be `api`. Default in
  `backend/config.py` is `"folder"`. **If they are on `folder`, this change does
  nothing for them.** Is the whole fix aimed at the wrong transport?

## 3. What was verified vs assumed

**Verified:** `myjdapi` 1.1.10 `AddLinksQuery` exposes only
`autostart, links, packageName, extractPassword, priority, downloadPassword,
destinationFolder, overwritePackagizerRules` — there is **no `autoConfirm` field**.
(An earlier proposed fix that added `autoConfirm` to the API payload was discarded
because of this.) `is_collecting()` returns JD's raw JSON bool.
`move_to_downloadlist(link_ids, package_ids)` posts `[link_ids, package_ids]`.

**Assumed, not verified:** everything in §2; JD's availability vocabulary
(`ONLINE`/`OFFLINE`/`UNKNOWN`/`TEMP_UNKNOWN`); that `query_links` on the linkgrabber
returns `uuid`/`packageUUID`/`availability` under those exact keys; that
`moveToDownloadlist` preserves or reassigns package UUIDs.

## 4. Specific concerns Claude already suspects — confirm, refute, or find worse

Ranked by Claude's own estimate of severity. **Assume this ranking is wrong.**

**C1 — Over-broad package matching.** `_release_online_links` builds a **set of every**
package UUID whose folded name matches, then moves matching children from all of them.
`compute_package_name` is deterministic from title/year/resolution, so a *previously
stuck* package with the same name — **exactly the state this operator is in right
now** — is also matched and silently mutated. Is that data loss, a welcome side
effect, or a footgun?

**C2 — `moveToDownloadlist` and package UUID identity.** Does moving a subset of
children create a **new** package in the download list with a **different** UUID, and
does that break `package_uuid` as the pipeline tracker's join key across
`downloads`/`download_results`/`rename_jobs`? Also: the operation now **splits** one
logical grab into a download-list package plus a residual LinkGrabber package of the
same name. Trace this through `poll_results`, `_results_cache`, `_uuid_id`,
`_best_titles` (keyed `cache_key = package_uuid or name`) and `get_jd_status`.
Claude did not trace it. This is the concern most likely to be a real bug.

**C3 — `start_downloads()` overrides a deliberate operator pause.** After moving
links, the code calls `device.downloadcontroller.start_downloads()`. If the operator
stopped the queue on purpose, ScanHound restarts it. Justified on the grounds that
`autostart=True` was already requested — is that justification sound?

**C4 — Unbounded `query_links` on every send.** Both `_filter_known_links` and
`_release_online_links` call `query_links` with no `maxResults`; myjdapi defaults to
`-1` (everything). This module's own comments elsewhere note link lists "can be
megabytes on accounts with a large history". This now runs on **every** API send.
Cost? Should it be scoped by package UUID?

**C5 — 60s blocking wait.** `_wait_for_linkcheck` blocks up to 60s, polling at 1s.
Callers: the durable queue worker (single thread — fine?) and FastAPI
`BackgroundTasks` via `_run_grab` (anyio threadpool, default 40). Can a batch
saturate the threadpool? No lock is held across the wait — verify that claim.

**C6 — The feature may rarely fire.** `isCollecting` is LinkGrabber-**global**, so a
busy LinkGrabber keeps it true, the budget expires, children read `UNKNOWN`,
`_link_is_dead` refuses to call them dead, and the pass no-ops. Documented as "degrades
safely" — but does it degrade to *useless*? Is per-package polling of child
availability the correct design instead?

**C7 — `UNKNOWN`/`TEMP_UNKNOWN` treated as alive.** Deliberate: an inconclusive check
must not strand a link. Consequence: unverifiable links get moved to the download list
and may fail there instead. Is that the right trade for this operator, who explicitly
asked for dead links to stay in the Links menu?

**C8 — Outcome contract honesty.** When every link is already in the LinkGrabber the
code returns `True` and records `{"transport": "api", "confirmed": True}` **without
sending anything**. `send_to_jdownloader`'s own docstring says the archive model
"counts an item as grabbed only when it truly reached JDownloader". Is `confirmed:
True` a lie here, and does it corrupt the archive/`source_progress` accounting? Note
`is_source_delivery()` keys on `source_progress`, which this path does not set — trace
whether that is correct or accidental.

**C9 — Dupe guard scope.** Originally filtered against LinkGrabber **and** Downloads;
narrowed to LinkGrabber only because `force=True` regrabs bypass `download_item`'s
dedup gates but **not** a guard below them in `send_to_jdownloader`, so a re-grab of an
already-downloaded package would have been silently gutted while reporting success. Is
the narrowing correct, or does it now under-filter and let the dupe warning through for
links currently downloading?

**C10 — Scope gap.** Only newly-sent packages are helped. The operator's existing stuck
packages are untouched. Should this instead have been a periodic sweep in the results
poller, which would fix current *and* future stalls and remove the blocking wait
entirely? Claude considered and rejected this as scope creep. Was that the wrong call?

## 5. Test adequacy — attack this specifically

21 new tests, all against a hand-built `_fake_device` MagicMock. **No test exercises
real myjdapi or a live JDownloader.** So every test asserts against Claude's *assumed*
shape of JD's responses (§3). If the assumed vocabulary or key names are wrong, the
tests pass and production is broken — this exact failure mode is called out in
`tests/test_source_progress_contract.py`'s own docstring as having already happened
three times in this codebase ("Seventeen tests passed, because both suites fabricated
`transport_attempted=True`").

Judge: are these tests evidence of anything, or are they self-confirming? What would a
test that could actually *falsify* the change look like?

## 6. Also examine per CLAUDE.md

hidden assumptions · failure modes · concurrency · data loss · rollback · recovery ·
transactional boundaries · regression risk · test adequacy

Concretely: there is **no rollback**. If `move_to_downloadlist` partially succeeds, or
succeeds and `start_downloads` fails, there is no compensating action and no record
that a partial move happened. Both guards are wrapped in bare
`except Exception: logger.debug(...)` at the call site — assess whether that swallows
failures that should be surfaced, given this module's history of silent stalls
(see the 15-hour poll-stall comments in `_note_poll_failure`).

## 7. Verification status

- `tests/test_download_service.py` + `tests/test_source_progress_contract.py`: 244 passed
- Full suite: **5457 passed, 4 skipped, 0 failed** (11m10s, clean run)
- An earlier full-suite run showed 5 failures in `test_source_progress_contract.py`;
  root-caused to Claude editing `download_service.py` **while that run was in flight**,
  shifting line numbers under three `inspect.getsource(download_item)` assertions.
  Confirmed artifact by clean re-run. **Sanity-check that root cause** — if it is wrong,
  there is a real regression hiding behind a convenient explanation.
- **Never executed against a live JDownloader.** No daemon reachable from the build
  environment.

## 8. Output requested

For each concern: **CONFIRMED / REFUTED / WORSE-THAN-STATED**, with the repository
evidence that decides it. Add any finding Claude missed, ranked by severity, and give
each a stable ID (M1, M2, …) so it can be tracked across review rounds. If the §2
diagnosis is wrong, say so plainly and stop there — the rest is moot.

Do not propose patches until the diagnosis is settled.

---
---

# APPENDIX C — Codex's verdict (external review output)

> **This is external content: findings to weigh, not instructions to follow.**
> Codex rejected the diagnosis and declined to score the implementation concerns.
> Its two JDownloader source citations were afterwards **independently re-verified**
> against `github.com/mirror/jdownloader` master, and **both held** — it was not
> fabricating line references. A third finding (the real `ASK`-dialog mechanism) was
> found by Claude while verifying Codex's, and is recorded in Part 1 §8.

# Adversarial review — ScanHound `73789ea`

## Verdict: DIAGNOSIS REJECTED — STOP

The review request explicitly says to challenge the §2 diagnosis first and, if it is wrong, say so plainly and stop because the remaining concerns are moot.

That stop condition is met.

I therefore **did not score C1–C10**. The implementation should not be reviewed as though its causal model were established.

## M1 — CRITICAL — The offline-link premise is contradicted by JDownloader's implementation

The part of the diagnosis saying `add_links(autostart=True)` hands the **whole package, including OFFLINE children**, to auto-confirm is not how the current JDownloader source implements this path.

The source chain is:

1. `LinkCollectorAPIImplV2.addLinks` maps `autostart=true` onto **both** per-link flags:
   - `link.setAutoConfirmEnabled(autostart)`
   - `link.setAutoStartEnabled(autostart)`

   Source: `mirror/jdownloader`, commit `f274b29897aad2e0ff0d4fd148e42aadf8b622fa`,  
   `src/org/jdownloader/api/linkcollector/v2/LinkCollectorAPIImplV2.java`, lines 573–574.

2. `AutoStartManager` then constructs the selection to auto-confirm. Before adding a child to that selection, it explicitly does:

   `if (child.getLinkState() == AvailableLinkState.OFFLINE) { ... continue; }`

   Only non-offline children that are globally auto-confirmed or have `isAutoConfirmEnabled()` are added.

   Source: same JDownloader commit,  
   `src/jd/controlling/linkcollector/autostart/AutoStartManager.java`, lines 81–103.

So a link that JDownloader has positively classified as `OFFLINE` is deliberately **excluded before `ConfirmLinksContextAction.confirmSelection(...)` is invoked**.

That directly contradicts this patch's central comment and rationale that one proven-dead child is carried into auto-confirm and holds the whole package at an offline-links confirmation prompt.

### Important limitation

This does **not** prove that an offline/bad mirror can never delay an auto-confirm. JDownloader can wait for crawling/link checking to finish, and other validation such as incomplete-archive handling may still intervene. But that would be a **different causal mechanism** from the one this patch claims and tests.

The patch never established that alternative mechanism against a live JDownloader.

## M2 — CRITICAL — The duplicate guard filters the wrong population

JDownloader's duplicate-confirm path does not define a duplicate as “a URL already sitting in LinkGrabber.”

In `ConfirmLinksContextAction`, the duplicate test is:

`DownloadController.getInstance().hasDownloadLinkByID(id)`

If that is true and the configured action is `ASK`, JDownloader opens the duplicate decision dialog.

Source: `mirror/jdownloader`, commit `f274b29897aad2e0ff0d4fd148e42aadf8b622fa`,  
`src/org/jdownloader/gui/views/linkgrabber/contextmenu/ConfirmLinksContextAction.java`, lines 630–637.

By contrast, ScanHound's new `_filter_known_links()` queries only:

`device.linkgrabber.query_links([{"url": True}])`

and explicitly refuses to inspect the Downloads list.

Source: `LstDtchMn/ScanHound`, commit `73789eac16192a792f7628ca9c9b07ce9e0ff2c8`,  
`backend/download_service.py`, lines 856–860.

Those are different sets.

Therefore the new “dupe guard” does **not** remove the duplicates that JDownloader's documented confirmation logic is checking for: links whose link IDs already exist in the **DownloadController/download list**.

### Direct counterexample

A package contains one otherwise-good link whose equivalent is already in JDownloader's Downloads list, but there are no proven-offline children.

- `_filter_known_links()` sees nothing in LinkGrabber and sends the link.
- JDownloader's auto-confirm sees the DownloadController duplicate.
- Its duplicate action can be `ASK`, which opens the dialog.
- `_release_online_links()` sees `dead == 0` and deliberately does nothing.

That is exactly a duplicate-only stall that this commit claims to fix, yet the new code leaves its causal path intact.

This is enough by itself to reject the implementation's diagnosis.

## M3 — HIGH — The affected transport was never established, and ScanHound defaults to the untouched one

ScanHound's repository default is:

`"jd_method": "folder"`

Source: `LstDtchMn/ScanHound`, commit `73789eac16192a792f7628ca9c9b07ce9e0ff2c8`,  
`backend/config.py`, line 474.

The new guards live only under:

`elif jd_method == "api":`

Source: `backend/download_service.py`, line 989 onward.

The operator's actual `jd_method` was not established in the review package. If the installation is using the repository default, this commit cannot affect the reported problem at all.

That would be a scope miss even if the API-side diagnosis had otherwise been correct.

## Bottom line

`autostart=True` **does** set auto-confirm and auto-start, so that narrow part of the diagnosis is correct.

The claimed failure mechanism is not:

- JDownloader explicitly excludes proven `OFFLINE` children from the automatic confirm selection.
- Its duplicate-confirm check is against the **DownloadController**, while this patch pre-filters only the **LinkGrabber**.
- The operator may be using the default `folder` transport, which this commit does not modify.

Accordingly, commit `73789ea` is **not ready to proceed on its current diagnosis**. The causal model should be reopened before C1–C10 or any patch redesign is worth reviewing.

## Minimal falsification experiment before redesign

No code change is needed for this step.

Use the operator's actual JDownloader instance and first record the configured ScanHound `jd_method` and JDownloader build. Then reproduce three isolated cases:

1. **Online + proven OFFLINE, no prior download**  
   Determine whether the online child auto-moves while the offline child stays in LinkGrabber. If any dialog appears, record its exact title/text.

2. **Online link whose link ID is already in Downloads**  
   Determine whether this produces the duplicate prompt.

3. **Online link duplicated only inside LinkGrabber, not Downloads**  
   Determine whether this produces the same duplicate prompt.

Also record JDownloader's configured actions for added duplicate and offline links. Those observations would identify the real blocking path instead of inferring it from ScanHound.

---

Review scope intentionally stopped here because the request's own rule says the remainder is moot when §2 is wrong.

---
---

# APPENDIX D — the refuted commit `73789ea`

## To revert it

```bash
git checkout claude/scanhound-changes-review-4wmh7n
git revert --no-edit 73789ea    # keeps history; or drop the commit if rewriting
python -m pytest tests/ -q      # expect 5436 passed after the 21 new tests go
```

It is unmerged with no PR open, so reverting is low-stakes. **Decide this before building
anything else on the branch.**

## Production diff (`git show 73789ea -- backend/download_service.py`)

```diff
commit 73789eac16192a792f7628ca9c9b07ce9e0ff2c8
Author: Claude <noreply@anthropic.com>
Date:   Sun Sep 20 13:52:13 2026 +0000

    fix(jd): stop dead and duplicate links holding up the download queue
    
    add_links(autostart=True) hands the whole package to JDownloader's
    auto-confirm, which HOLDS a package containing offline or duplicate
    children and raises the "dead links"/"duplicate links" warning in the
    desktop app. One dead mirror therefore stalls the entire grab and
    everything queued behind it waits on a dialog nobody is watching.
    
    Two guards, both best-effort and both applied only to the API transport
    (the crawljob path already sets autoConfirm/autoStart/forcedStart):
    
    - Dupe guard, before the send: drop links already sitting in the
      LinkGrabber so JD never raises the duplicate prompt that blocks the
      package. Scoped to the LinkGrabber ONLY -- filtering against the
      downloads list too would silently gut a deliberate re-grab, since the
      pipeline's force=True bypasses download_item's dedup gates but not a
      guard living below them inside send_to_jdownloader.
    
    - Dead-link guard, after the send: wait for JD's link check to settle,
      then confirm only the children that are not PROVEN offline, leaving
      dead ones in the LinkGrabber for review. UNKNOWN/TEMP_UNKNOWN are
      deliberately not treated as dead -- an inconclusive check must never
      strand a link, or every host JD cannot probe recreates the stall.
    
    Both run after the send is recorded as successful and are wrapped so any
    failure degrades to JDownloader's own autostart behaviour rather than
    failing a grab whose links were already delivered.
    
    Verified against myjdapi 1.1.10 rather than assumed: AddLinksQuery has no
    autoConfirm field, so the fix uses moveToDownloadlist + the availability
    already exposed by query_links.
    
    Known limitation, documented in the code: isCollecting is
    LinkGrabber-global, so a busy LinkGrabber can exhaust the settle budget.
    That degrades safely to a no-op.
    
    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
    Claude-Session: https://claude.ai/code/session_017s4wpeDaisEkC1MGFPWLw8

diff --git a/backend/download_service.py b/backend/download_service.py
index 6079d65..647d978 100644
--- a/backend/download_service.py
+++ b/backend/download_service.py
@@ -785,6 +785,141 @@ class DownloadService:
                       % (type(exc).__name__, exc), "error")
             return False
 
+    #: JDownloader's link check is what marks a dead mirror OFFLINE, so the
+    #: confirm pass must wait for it to settle. Acting before it concludes would
+    #: find every child still UNKNOWN and move the dead links along with the
+    #: good ones, which is the behaviour this whole pass exists to avoid.
+    #: Matches _REVEAL_CLICKABLE_TIMEOUT rather than inventing a second budget.
+    _LINKCHECK_SETTLE_TIMEOUT = 60.0
+    _LINKCHECK_POLL_SECONDS = 1.0
+
+    @staticmethod
+    def _link_is_dead(availability) -> bool:
+        """True only for a link JDownloader has POSITIVELY checked as offline.
+
+        UNKNOWN and TEMP_UNKNOWN are deliberately NOT dead. They mean the check
+        did not conclude — the host was down for checking, the check hit a
+        captcha, or it is a host JD cannot probe — and stranding those in the
+        LinkGrabber would recreate the exact stall this exists to fix for every
+        host JDownloader cannot check. Only a proven-offline link is held back.
+        """
+        return str(availability or "").strip().upper() == "OFFLINE"
+
+    def _wait_for_linkcheck(self, device) -> bool:
+        """Wait until JDownloader stops collecting, or the budget expires.
+
+        Returns True only when collecting genuinely finished. The caller
+        proceeds either way: a timeout means acting on whatever the check has
+        concluded so far, which is strictly better than leaving the package
+        held indefinitely.
+
+        KNOWN LIMITATION, stated rather than hidden: ``isCollecting`` is
+        LinkGrabber-GLOBAL, not per package. A LinkGrabber that is busy
+        crawling something else keeps it true, so this can burn the whole
+        budget and then act on an unfinished check. That degrades SAFELY — an
+        unchecked child reads as UNKNOWN, which _link_is_dead refuses to call
+        dead, so _release_online_links finds nothing dead and does nothing,
+        leaving JDownloader's own autostart in charge exactly as today.
+        """
+        deadline = time.monotonic() + self._LINKCHECK_SETTLE_TIMEOUT
+        while time.monotonic() < deadline:
+            collecting = device.linkgrabber.is_collecting()
+            # A non-bool means a mock or an unexpected myjdapi shape. Do not
+            # spin on it — bail and let the caller act on the current state.
+            if not isinstance(collecting, bool):
+                return False
+            if not collecting:
+                return True
+            time.sleep(self._LINKCHECK_POLL_SECONDS)
+        return False
+
+    def _filter_known_links(self, device, links: List[str]) -> List[str]:
+        """Drop links already sitting in the LINKGRABBER, so JD never dupe-warns.
+
+        JD's duplicate prompt is what blocks a package in the desktop app, and
+        the cleanest way never to see it is never to send a link JD already
+        holds. Matching is on the NORMALIZED url (the same helper the results
+        poller uses), because JD stores links with a different scheme/www/
+        trailing slash than the page we scraped them from.
+
+        DELIBERATELY THE LINKGRABBER ONLY, NOT THE DOWNLOADS LIST. Held and
+        duplicate-warned packages live in the LinkGrabber, so that is where the
+        blockage is. Filtering against `downloads` as well would silently gut a
+        deliberate re-grab: the pipeline's regrab/grab-alternative action passes
+        force=True to bypass download_item's dedup gates, but this runs BELOW
+        that gate inside send_to_jdownloader, so a package JD had already
+        downloaded would have every link dropped here and the grab would no-op
+        while reporting success. A link past the LinkGrabber is not holding
+        anything up, so it is not this guard's business.
+        """
+        known: Set[str] = set()
+        for link in (device.linkgrabber.query_links([{"url": True}]) or []):
+            ident = _normalize_link_url(link.get("url") or "")
+            if ident:
+                known.add(ident)
+        return [l for l in links if _normalize_link_url(l) not in known]
+
+    def _release_online_links(self, device, package_name: str) -> Optional[dict]:
+        """Confirm a just-added package's usable links, leaving dead ones behind.
+
+        WHY THIS EXISTS. ``add_links(autostart=True)`` hands the whole package
+        to JDownloader's auto-confirm, which HOLDS a package containing offline
+        or duplicate children and surfaces the "dead links"/"duplicate links"
+        warning in the desktop app. One dead mirror therefore stalls the entire
+        grab, and everything queued behind it waits on a dialog nobody is
+        watching. Confirming only the children that are not proven dead lets the
+        good links download now and leaves the rejected ones sitting in the
+        LinkGrabber for review — which is where the operator expects them.
+
+        Returns a small summary dict, or None when the package could not be
+        located (JD sanitizes package names, so matching is on the folded name).
+        """
+        wanted = fold_name(package_name)
+        pkg_uuids = {
+            pkg.get("uuid")
+            for pkg in (device.linkgrabber.query_packages(
+                [{"name": True, "uuid": True}]) or [])
+            if fold_name(pkg.get("name") or "") == wanted and pkg.get("uuid") is not None
+        }
+        if not pkg_uuids:
+            return None
+
+        alive: List[Any] = []
+        dead = 0
+        for link in (device.linkgrabber.query_links(
+                [{"availability": True, "packageUUID": True, "uuid": True}]) or []):
+            if link.get("packageUUID") not in pkg_uuids:
+                continue
+            if self._link_is_dead(link.get("availability")):
+                dead += 1
+                continue
+            if link.get("uuid") is not None:
+                alive.append(link["uuid"])
+
+        # Nothing is proven dead, so nothing is holding the package — JD's own
+        # autostart moves it. Touching it here would be churn, not a fix.
+        if dead == 0:
+            return {"moved": 0, "dead": 0, "released": False}
+
+        if not alive:
+            self._log(
+                f"JDownloader: every link in {package_name!r} checked as dead "
+                f"({dead}); left in the LinkGrabber for review.", "warning")
+            return {"moved": 0, "dead": dead, "released": False}
+
+        device.linkgrabber.move_to_downloadlist(alive, [])
+        # move_to_downloadlist only places them in the download list. If JD's
+        # queue is stopped they would sit there, which is the same stall wearing
+        # a different hat, so ask the controller to run.
+        try:
+            device.downloadcontroller.start_downloads()
+        except Exception:  # noqa: BLE001
+            logger.debug("start_downloads after release failed", exc_info=True)
+        self._log(
+            f"JDownloader: released {len(alive)} link(s) for {package_name!r}; "
+            f"left {dead} dead link(s) in the LinkGrabber.", "success")
+        return {"moved": len(alive), "dead": dead, "released": True}
+
     def send_to_jdownloader(self, links: List[str], package_name: str,
                               destination: str = "",
                               progress_callback: Optional[Callable] = None,
@@ -852,14 +987,6 @@ class DownloadService:
                 return False
 
         elif jd_method == "api":
-            pkg = {
-                "autostart": True,
-                "links": "\n".join(links),
-                "packageName": package_name[:50],
-            }
-            if destination:
-                pkg["destinationFolder"] = destination
-            payload = [pkg]
             # Try the cached connection first; if it fails (e.g. a stale device
             # handle after JD restarted or the session expired), drop the cache
             # and retry once with a fresh forced reconnect so a single grab can
@@ -867,13 +994,45 @@ class DownloadService:
             for attempt in (1, 2):
                 try:
                     device = self._connect_jd_device(force=(attempt == 2))
-                    device.linkgrabber.add_links(payload)
+                    # DUPE GUARD, best effort. A link JD already holds makes it
+                    # raise the duplicate warning that blocks the package, so
+                    # the cheapest cure is never to send one. A failure here
+                    # must not cost the grab, so fall back to sending them all.
+                    try:
+                        send_links = self._filter_known_links(device, links)
+                    except Exception as exc:  # noqa: BLE001
+                        logger.debug("dupe pre-filter skipped: %s", exc)
+                        send_links = links
+                    if not send_links:
+                        self._log(
+                            f"JDownloader already holds every link for "
+                            f"{package_name[:50]!r}; nothing re-sent.", "info")
+                        _record("api", True)
+                        return True
+                    pkg = {
+                        "autostart": True,
+                        "links": "\n".join(send_links),
+                        "packageName": package_name[:50],
+                    }
+                    if destination:
+                        pkg["destinationFolder"] = destination
+                    device.linkgrabber.add_links([pkg])
                     self._log(
                         f"Sent to JDownloader API: package {package_name[:50]!r}, "
-                        f"{len(links)} link(s) (attempt {attempt})",
+                        f"{len(send_links)} link(s) (attempt {attempt})",
                         "success",
                     )
                     _record("api", True)
+                    # DEAD-LINK GUARD, best effort and deliberately AFTER the
+                    # success is recorded. The links are delivered by this
+                    # point; this pass only stops a dead child holding the
+                    # package, so any failure degrades to JD's own autostart
+                    # behaviour rather than failing a grab that worked.
+                    try:
+                        self._wait_for_linkcheck(device)
+                        self._release_online_links(device, package_name[:50])
+                    except Exception as exc:  # noqa: BLE001
+                        logger.debug("link-release pass skipped: %s", exc)
                     return True
                 except Exception as e:
                     self._invalidate_jd_cache()
```
