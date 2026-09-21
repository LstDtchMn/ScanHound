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

> **STATUS: REVERTED** (owner decision, 2026-09-21) by `24d0897`. The section below is kept as
> the record of *why*, because the real mechanism it uncovered — the JD `ASK` dialog — is still
> the live remediation for the dupe/dead-link complaint. The revert removed 394 lines: both
> guards and their 21 tests. **The operator's original complaint is NOT fixed by the revert; it
> is fixed by the two JDownloader settings in §4.**

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
24d0897  Revert "fix(jd): stop dead and duplicate links ..."   ← owner decision
4645afb  docs: add self-contained full briefing
c82808e  docs: complete the handoff with architecture, queue states, runbook
0b45d6a  docs: expand session handoff with cross-session and repo context
25f8b70  docs: add session handoff for continuing work locally
73789ea  fix(jd): stop dead and duplicate links ...            ← REFUTED, now reverted
0a2751d  Merge pull request #59  (origin/main HEAD, 2026-08-28)
```
Net effect on code: **none** — `73789ea` and its revert cancel out, so the branch carries
documentation only. Nothing here changes runtime behaviour.
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
5. ~~Keep or revert `73789ea`?~~ **RESOLVED — reverted by `24d0897`.**
6. Did the Plex repoint work conclude, and does it bear on the stuck queue?

## 15. Do not

- Do not re-apply `73789ea` — its causal model is refuted and it has been reverted (`24d0897`).
- Do not bulk-retry interrupted items before checking JD for already-delivered packages.
- Do not use the `_AND_REMOVE` JD actions; dead/dupe links should **stay** in the Links menu.
- Do not act on `docs/reviews/STATE-OF-PLAY.md` §1 without re-verifying — it is stale.
- Do not edit source files while a test suite is running (§11).
- Do not ship a fix on an unverified causal model. That is exactly what `73789ea` did.
