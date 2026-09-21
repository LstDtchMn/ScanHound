# ScanHound — operational handoff to a local session

**Assembled:** 2026-09-21 · **Repo:** LstDtchMn/ScanHound
**Branch:** `claude/scanhound-changes-review-4wmh7n` (documentation-only; code is identical to `main`)

> **You are the only one who can reach the server.** The owner has no access right now and cannot
> run commands or relay output. This file was written by a cloud-sandbox session that also had no
> access. **Nothing in it was executed against the live instance.** Every claim is marked either
> *verified from source* (with file:line) or *unverified*. You are the first party in this chain
> who can actually observe reality — so verify before you act, and trust observation over anything
> asserted here.

---

## 0. Your mandate

The owner was asked how much authority to grant you. These are their answers, not defaults.

### You MAY do these without asking

- Every read-only diagnostic in §2 (`/health`, SQLite queries, JD state/config/dialog reads).
- **Change JDownloader's dupes/offline actions from `ASK` to `EXCLUDE` / `EXCLUDE_OFFLINE`.**
  This is the behaviour the owner explicitly asked for, in their words: *"by default duplicate
  links and dead links should be skipped and remain in the links only menu."*
- **Answer a blocking JDownloader dialog** to unblock the queue, choosing the option consistent
  with that same intent (exclude, do not delete).
- Read anything in the repo; run the test suite.

### You MUST STOP and ask before

- Anything that could **duplicate a download** — see §4 for the one procedure that is pre-approved.
- Anything that **deletes** data: links, packages, DB rows, files.
- Any `_AND_REMOVE` JDownloader action.
- Restarting or reconfiguring the container beyond the above.
- Committing code changes to fix a cause you have not yet proven.

### How to escalate

The owner is away. Use the **interactive question dialog** (`AskUserQuestion`) — per `CLAUDE.md`,
every option must carry its meaning and tradeoff, recommendation first — then **stop and wait**.
Do not proceed on a guess because nobody answered. A stalled queue is recoverable; a few hundred
duplicate grabs are not.

---

## 1. The situation

Two separate things are live. Do not conflate them.

| | Problem | Status |
|---|---|---|
| **A** | **Download queue is stuck.** Reported right after the Docker container was **moved from one PC to another**. | Undiagnosed. Leading hypothesis verified *in code*, never observed. |
| **B** | **Dead/duplicate links stall JDownloader**, showing a warning in the desktop app that "holds up the other downloads". | Root cause identified and verified against JD source. **Not yet fixed.** |

A previous session shipped a fix for **B** (`73789ea`) built on an unverified causal model. It was
refuted by adversarial review and **has been reverted**. The branch now contains documentation
only. See §6 — the reasoning there is still valuable even though the code is gone.

---

## 2. Step 1 — Diagnose (read-only, do this first)

```bash
# 1. Is the app alive, and which JD transport is live?
docker exec scanhound curl -s localhost:9721/health | python3 -m json.tool
```

`/health` is deliberately unauthenticated (`backend/api/routes/system.py:17`). Everything else
needs auth.

| Field | Meaning |
|---|---|
| `jd_enabled` | `jd_enabled AND jd_method == "api"`. **If false, the JD API path is not in use** — either JD is off or `jd_method` is `folder` (the repo default, `config.py:474`). This single field resolves a long-open question. |
| `queue.executor_starved` | Work is due, nothing **started** — scheduler/ownership/liveness fault |
| `queue.source_no_progress` | Attempts running, source delivering nothing — source fault |
| `queue.human_required` | Verification hold, or auto-resume exhausted — **no timer clears this** |
| `jd_poll.failure_phase` | `connect` / `update_devices` / `get_device` / `query_packages` |
| `jd_poll.stalled_seconds` | Seconds since JD last answered |

Three stall conditions are separated deliberately: one timer cannot distinguish *"nothing was
attempted"* from *"everything attempted failed"* (`database.py:6069`).

`jd_poll` triage (`download_service.jd_poll_health`):

| Signature | Diagnosis |
|---|---|
| `cycles_started == cycles_completed`, static | Poller thread **stopped** |
| `cycles_started > cycles_completed`, `current_cycle_seconds` growing | **Blocked** mid-call |
| Both advancing, `stalled_seconds` growing | Cycling but **failing** — read `failure_phase` |

```bash
# 2. Confirm the leading hypothesis. Verify the DB path first: docker exec scanhound ls /data
docker exec scanhound sqlite3 /data/.local/share/scanhound/scanhound.db \
  "SELECT last_reason_code, state, COUNT(*) FROM download_queue_items
   GROUP BY last_reason_code, state ORDER BY 3 DESC;"

# 3. Are packages piling up unconfirmed in JD?
docker exec scanhound curl -s localhost:9721/download/jd-status \
  | python3 -m json.tool | grep -c '"stage": "linkgrabber"'
```

**`HOME=/data`** in the container, so config is `/data/.config/scanhound` and data
(SQLite DB, Plex cache, logs) is `/data/.local/share/scanhound`. Host mount is `./data`.

---

## 3. Step 2 — The JDownloader API toolkit

**This is the most useful thing in this document and it was discovered late.** `myjdapi` 1.1.10
exposes far more than ScanHound uses. Since nobody can reach the JD Windows GUI, everything below
is your substitute for it. Verified by introspecting the installed library.

```python
device = download_service._connect_jd_device()   # or build a Myjdapi() session directly
```

| API | Use |
|---|---|
| `device.dialogs.list()` | **Enumerate open dialogs.** If JD is sitting on a blocking modal, this proves it outright — no inference needed. |
| `device.dialogs.get(id)` | Read one dialog's text/options |
| `device.dialogs.answer(id, data)` | **Answer it and unblock JD** |
| `device.config.query([...])` / `config.list()` | **Discover** config interfaces and keys |
| `device.config.listEnum(type)` | List an enum's legal values |
| `device.config.get(iface, storage, key)` | Read a setting |
| `device.config.set(iface, storage, key, value)` | **Write a setting** |
| `device.downloadcontroller.get_current_state()` | running / paused / stopped |
| `device.downloadcontroller.force_download(link_ids, package_ids)` | Force specific items |
| `device.linkgrabber.query_packages/query_links` | Inspect the LinkGrabber |
| `device.linkgrabber.move_to_downloadlist(link_ids, package_ids)` | Confirm links |

> ⚠️ **Do not guess the config interface name or keys. Discover them.**
> Use `config.query()` / `config.list()` and search for the LinkGrabber settings, then
> `listEnum` for the legal values. The previous session's central mistake was asserting an API
> field (`autoConfirm` on `AddLinksQuery`) that does not exist. Verify, then act.
>
> The keys you are looking for correspond to what JD's source calls
> `getDefaultOnAddedDupesLinksAction()` and `getDefaultOnAddedOfflineLinksAction()`.
> Target values: `EXCLUDE` and `EXCLUDE_OFFLINE`. **Never** the `_AND_REMOVE` variants —
> the owner wants these links *kept* in the Links menu, not deleted.

---

## 4. Step 3 — Remediate

### Problem B — dead/duplicate links (pre-approved, go ahead)

1. `device.dialogs.list()`. If a dupes/offline modal is open, that is the blockage. Answer it with
   the exclude option (never delete).
2. Discover and set the two LinkGrabber actions to `EXCLUDE` / `EXCLUDE_OFFLINE` so it does not
   recur. **Report the before values** — they are evidence for whether this was the cause.
3. Re-check `/download/jd-status`; the `linkgrabber`-stage count should fall as packages confirm.

### Problem A — the stuck queue

**Hypothesis A — interrupted claims ⭐ VERIFIED IN CODE, most likely**

Moving the container stops the process. On startup `recover_interrupted()` rewrites **every** row
in state `claimed` — `backend/download_queue.py:276-295`:

```sql
UPDATE download_queue_items
SET state = 'failed', queue_reason = 'manual_retry',
    last_reason_code = 'interrupted_unknown_outcome',
    transport_attempted = 1, ...
WHERE state = 'claimed'
```

Those rows are then **permanently excluded from automatic claiming** — verified at three sites:
`_claim_due()` at `752-755` and `863-866`, auto-resume at `1935-1938`:

```sql
AND COALESCE(last_reason_code, '') NOT IN (
    'operation_timeout_unknown', 'interrupted_unknown_outcome')
```

**This is deliberate, not a bug.** An interrupted item's delivery outcome is unknown; auto-retrying
risks double-submitting a grab that already reached JDownloader.

#### The approved procedure for these items

The owner chose **cross-check, then retry only the clean ones**:

1. List the stranded rows (`last_reason_code = 'interrupted_unknown_outcome'`) with their titles,
   URLs and `package_name`.
2. For each, check whether **JDownloader already holds a matching package** — `jd-status`, or
   `linkgrabber`/`downloads` `query_packages`. `compute_package_name()` in `download_service.py`
   is the canonical name, and `fold_name()` is the comparison helper that survives JD's character
   substitutions. Use both; do not hand-roll matching.
3. **Retry only rows with no JD counterpart** — `POST /download/retries/{item_uuid}/retry`.
   Both retry paths clear the blocking reason code (`download_queue.py:2286-2291`, `2366-2372`),
   setting `last_reason_code = NULL` and state `ready`.
4. **Leave the rest**, and report them with the evidence for each. Do not bulk-retry the remainder.

This is the manual adjudication the exclusion exists to force — performed carefully, not skipped.

**Hypothesis C — stale `jd_device` after the move** (unverified)
`_connect_jd_device()` calls `jd.get_device(device_name)` from config `jd_device` and **raises** if
the name is not found. If JD was reinstalled or renamed on the new PC, every send and poll fails.
Signature: `jd_poll.failure_phase == "get_device"`. Fix is a config change — confirm the real device
name via `jd.list_devices()` first.

**Hypothesis D — host-dependent config broken by the move** (unverified)
`docker-compose.yml` maps `host.docker.internal:host-gateway` for Plex `:32400` and joins an
**external** `proxy` network. Verify the `proxy` network exists on the new host, Plex is reachable,
and `./data` survived with the SQLite DB intact.

**Hypothesis E — Plex repoint** (cross-session)
A parallel session *"Plex library repoint adversarial review"* was active 2026-09-21 01:20 —
*"six hypotheses eliminated; traps set for event-centric correlation."* If Plex paths moved with the
server, that work overlaps. Check it before re-deriving its conclusions.

**If `queue.human_required` is true:** that is a verification hold, not the above. No timer releases
it. `GET /download/retries` lists held items; `POST /download/verification-hold/clear` is the
operator escape hatch — treat using it as a "must ask" action.

---

## 5. Architecture

**Stack:** FastAPI backend + Svelte 5 frontend, one origin, one port (**9721**), SQLite, Docker.
A Tauri v2 Android app shares the frontend.

| Area | Modules (`backend/`) |
|---|---|
| **API** | `api/main.py` (app + auth middleware), `api/dependencies.py` (`ServiceRegistry`, `auth_enabled`, `token_authorized`), `api/ws.py`, `api/routes/*.py` |
| **Scanning** | `scanner_service.py`, `background_scanner.py`, `link_scraper.py`, `detail_scraper.py`, `metadata_enricher.py` |
| **Sources** | `sources/` — `hdencode*.py`, `ddlbase.py`, `adithd.py`, `registry.py`; identity unified in `source_identity.py` |
| **Downloads** | `download_service.py` (JD transport, scraping, poller — **~4200 lines**), `download_queue.py` (durable queue + scheduler), `download_outcome.py`, `queue_recovery_policy.py`, `clicknload.py` |
| **Rename** | `rename/` — `service.py`, `naming.py`, `fileops.py`, `confidence.py`, `llm_identify.py`, `dv_detect.py`, `conflicts.py` |
| **Plex** | `plex_manager.py`, `plex_service.py`, `plex_metadata_scan.py` |
| **Data** | `database.py` (**very large** — all schema + queries), `models.py`, `config.py` |

**Route prefixes:** `/auth` `/scan` `/results` `/download` `/rename` `/plex` `/settings` `/sources`
`/rss` `/watchlist` `/analytics` `/pipeline` `/scheduler` `/background`, plus unprefixed `/health`,
`/discover`, `/shutdown`.

**Frontend:** routes `/` (scan results) `downloads` `renames` `pipeline` `analytics`
`media-inventory` `rss` `settings` `login`; stores in `lib/stores/`, components in
`lib/components/`, client in `lib/api/`. Svelte 5 runes.

**Config — the 4-place pattern.** A new setting touches four places or it silently does nothing:
`config.py` `AppConfig` → `config.py` defaults → `api/routes/settings.py` `SettingsUpdate`
(Pydantic, `extra="forbid"`) → the Settings UI tab.

**Networking.** No host port is published. Reachable only via the external `proxy` network as
`http://scanhound:9721`, fronted by Nginx Proxy Manager + Cloudflare Tunnel. **The container has
no built-in auth** — Cloudflare Access / NPM Access Lists are the authentication layer.

---

## 6. The download queue state machine

`download_queue_items.state` (`database.py:1379`):

| State | Meaning |
|---|---|
| `scheduled` | Waiting for its due time |
| `ready` | Due, promoted (manual retry, or item-local deferral) |
| `claimed` | A worker owns it under a lease. **Interrupted claims become `failed` on restart — §4** |
| `waiting_source` | Parked by a source-wide pause/cooldown |
| `verification_required` | Behind an interactive challenge — **only a human probe releases it** |
| `completed` / `failed` / `cancelled` | Terminal |

`queue_reason` (`database.py:1375`): `user_batch` · `interactive_challenge` · `source_deferred` ·
`manual_retry`
`download_queue_batches.state` (`database.py:1288`): `scheduled` · `running` · `paused_source` ·
`waiting_user` · `completed` · `cancelled`

**Invariants — each learned from an incident. Read the code comments before "simplifying" anything.**
- Source pacing is **global per source**, not per batch (capacity vs demand).
- A verification hold is **source-wide**; a timer never releases it.
- Auto-resume has a **budget**; exhausted batches stop and require manual action.
- `operation_timeout_unknown` / `interrupted_unknown_outcome` are **never auto-retried**.
- Every terminal write carries an ownership predicate (`AND claimed_by = ?`) so a stale worker
  cannot overwrite a newer one.

---

## 7. Background — the refuted fix (`73789ea`, reverted)

Kept because the investigation produced the fix for Problem B, even though the code is gone.

**Claimed premise:** `add_links(autostart=True)` carries proven-OFFLINE children into auto-confirm,
so one dead mirror holds the package. **Codex rejected it.** Both citations were then independently
re-verified against `github.com/mirror/jdownloader` master, and **both held**:

| ID | Finding | Evidence | Status |
|---|---|---|---|
| **M1** | JD already excludes offline children | `AutoStartManager.java`: `if (child.getLinkState() == AvailableLinkState.OFFLINE) { createNewSelection = true; continue; }` | **CONFIRMED** — the reverted code reimplemented what JD does natively |
| **M2** | Dupe test is against the *download list*, not the LinkGrabber | `ConfirmLinksContextAction.java`: `} else if (!DownloadController.getInstance().hasDownloadLinkByID(id)) { continue; }` | **CONFIRMED, worse than stated** — the filter had been narrowed to LinkGrabber-only, hitting exactly the wrong population |
| **M3** | Affected transport never established; repo default is `folder` | `config.py:474` | **OPEN** — resolve via `/health` `jd_enabled` |

**The real mechanism**, found while verifying the above: `ConfirmLinksContextAction` reads two
configurable settings, `getDefaultOnAddedDupesLinksAction()` and
`getDefaultOnAddedOfflineLinksAction()`, each of which can be **`ASK`** — which opens a modal
dialog. That dialog blocks JD's GUI confirm path but **not** its API, so `jd_poll` can read
perfectly healthy while packages pile up unconfirmed. **The fix is JD configuration (§3–4), and it
is transport-independent, which also dissolves M3.**

Also verified: `myjdapi` 1.1.10 `AddLinksQuery` has **no `autoConfirm` field** — only `autostart,
links, packageName, extractPassword, priority, downloadPassword, destinationFolder,
overwritePackagizerRules`.

---

## 8. Running it

```bash
docker compose up -d --build        # the only supported deploy
python -m backend.api --port 9721   # backend dev
cd frontend && npm run dev          # frontend dev
python3 -m pytest tests/ -q         # full suite ≈ 11 min → expect 5436 passed, 4 skipped
cd frontend && npm run test:unit && npm run check
```

**Test deps are hand-curated in `.github/workflows/tests.yml`, NOT all in `requirements.txt`:**
`pytest pytest-asyncio requests aiohttp cloudscraper beautifulsoup4 thefuzz rapidfuzz python-dotenv
packaging selenium PlexAPI fastapi uvicorn httpx bcrypt websockets plyer myjdapi`

---

## 9. Traps that have already cost time

- ⚠️ **`inspect.getsource`.** `tests/test_source_progress_contract.py` asserts on the *source text*
  of `download_item`. Editing `backend/download_service.py` **while a suite runs** shifts line
  numbers and yields 5 spurious failures. This happened and was briefly misdiagnosed as a
  regression. **Never edit Python while a suite is running.**
- ⚠️ **Module-global test isolation.** Module-level globals have repeatedly leaked between test
  modules (e.g. `_last_scan_items` in `api/routes/scanner.py`). Tests pass isolated, fail only in
  the full suite. **Always verify with a full run.**
- ⚠️ **Do not assert an API exists without checking.** The `autoConfirm` mistake above is the
  canonical example. Introspect the library or read the source.
- **`CLAUDE.md` mandates Codex adversarial review** for non-trivial changes — ask it to *challenge*,
  not confirm; preserve finding IDs across rounds; **never claim Codex reviewed something unless it
  did.** `73789ea` is the live demonstration of why.
- **Commit attribution:** end messages with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` plus the `Claude-Session:` line.
- **`docs/reviews/STATE-OF-PLAY.md` is STALE.** Dated 2026-08-10, it warns `main`'s `dv_detect.py`
  can remove a managed Plex label and points at `agent/dv-detector-consolidation` — a branch that no
  longer exists on origin. `main` advanced to 2026-08-28 and `dv_detect.py` was rewritten. **Do not
  act on that warning without re-verifying.**

---

## 10. Report back with

1. `/health` in full — especially `jd_enabled`, the three `queue.*` flags, `jd_poll`.
2. The SQLite `last_reason_code` / `state` breakdown.
3. `device.dialogs.list()` — was a modal actually blocking JD?
4. The **before** values of JD's dupes/offline actions, and what you set them to.
5. Which interrupted items you retried, which you left, and the JD evidence for each.
6. Whether the queue is moving now, and which hypothesis it actually was.

## 11. Do not

- Do not re-apply `73789ea` — refuted and reverted.
- Do not bulk-retry interrupted items. Cross-check first (§4).
- Do not use `_AND_REMOVE` JD actions — dead/dupe links must **stay** in the Links menu.
- Do not delete links, packages, DB rows or files without asking.
- Do not edit Python while a test suite is running.
- Do not ship a fix on an unverified causal model — that is exactly what `73789ea` did.
- Do not proceed on a guess because the owner did not answer. Ask, then wait.
