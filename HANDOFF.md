# ScanHound — session handoff

**Written:** 2026-09-21 · **Repo:** LstDtchMn/ScanHound · **Branch:** `claude/scanhound-changes-review-4wmh7n`

You are picking this up cold in a **local** Claude Code session on the PC that now runs the
ScanHound Docker container. The previous session ran in an isolated cloud sandbox with **no
route to this machine**, so everything below is either (a) verified from source, or (b)
explicitly flagged as unverified. Nothing was ever executed against the live instance.

Read §1 and §2 first. There is a live production problem.

---

## 1. PRIORITY: downloads are currently stuck

Operator report: *"It seems like a lot of downloads are currently stuck."*

Critical context given afterwards: **the container was moved from one PC to another.**

### Hypothesis A — interrupted claims (VERIFIED IN CODE, ranked first)

Moving the container stops the process. On startup `DownloadQueueService.recover_interrupted()`
takes **every** row in state `claimed` and rewrites it:

`backend/download_queue.py:276-295`
```sql
UPDATE download_queue_items
SET state = 'failed',
    queue_reason = 'manual_retry',
    last_reason_code = 'interrupted_unknown_outcome',
    transport_attempted = 1, ...
WHERE state = 'claimed'
```

Those rows are then **permanently excluded from automatic claiming** — `_claim_due()` filters
them at `download_queue.py:752-755` and again at `863-866`, and auto-resume excludes them at
`1935-1938`:

```sql
AND COALESCE(last_reason_code, '') NOT IN (
    'operation_timeout_unknown',
    'interrupted_unknown_outcome'
)
```

This is **deliberate, not a bug**: the delivery outcome of an interrupted item is unknown, and
retrying could double-submit something that already reached JDownloader. But it means a
container move strands every in-flight item permanently until a human acts.

**This fits the symptom exactly and is the first thing to check.**

### Hypothesis B — JDownloader dupe/offline `ASK` dialog (VERIFIED against JD source)

See §4. If JD's LinkGrabber is configured to `ASK` on added dupes or offline links, it opens a
**modal dialog**. That blocks JD's GUI confirm path but *not* its API — so `jd_poll` looks
healthy while packages pile up unconfirmed in the LinkGrabber.

### Hypothesis C — stale `jd_device` after the move (UNVERIFIED)

`_connect_jd_device()` (`download_service.py`) calls `jd.get_device(device_name)` using config
key `jd_device`, and **raises** if that name is not found. If JDownloader was reinstalled or
renamed on the new PC, every send and every poll fails. Check `jd_poll.failure_phase` — it will
read `get_device`.

### Hypothesis D — host-dependent config broken by the move (UNVERIFIED)

`docker-compose.yml` maps `host.docker.internal:host-gateway` for Plex on `:32400`, and joins an
**external** `proxy` network. On a new host, verify: the `proxy` network exists, Plex is
reachable, and `./data` came across with the SQLite DB intact.

---

## 2. Diagnostic playbook

`/health` is **deliberately unauthenticated** so a watchdog can read it without a credential.

```bash
docker exec scanhound curl -s localhost:9721/health | python3 -m json.tool
```

| Field | Meaning |
|---|---|
| `jd_enabled` | Computed as `jd_enabled AND jd_method == "api"`. **This resolves the open question of which transport is live.** |
| `queue.executor_starved` | Work is due, nothing has **started** — scheduler/ownership/liveness fault |
| `queue.source_no_progress` | Attempts running, source delivering nothing — source fault |
| `queue.human_required` | Verification hold, or deferred work with auto-resume off — **no timer clears this** |
| `jd_poll.failure_phase` | Which step failed: `connect` / `update_devices` / `get_device` / `query_packages` |
| `jd_poll.stalled_seconds` | Time since JD last answered |

`jd_poll` liveness triage (`download_service.jd_poll_health`):

| Signature | Diagnosis |
|---|---|
| `cycles_started == cycles_completed`, static | Poller thread **stopped** |
| `cycles_started > cycles_completed`, `current_cycle_seconds` growing | **Blocked** mid-call |
| Both advancing, `stalled_seconds` growing | Cycling but **failing** — read `failure_phase` |

Confirm Hypothesis A by counting stranded rows:
```bash
docker exec scanhound sqlite3 /data/.local/share/scanhound/scanhound.db \
  "SELECT last_reason_code, state, COUNT(*) FROM download_queue_items
   GROUP BY last_reason_code, state ORDER BY 3 DESC;"
```
*(Verify the DB path first — `ls /data` inside the container. `HOME=/data`, so config lives in
`/data/.config/scanhound` and data in `/data/.local/share/scanhound`.)*

Confirm Hypothesis B:
```bash
docker exec scanhound curl -s localhost:9721/download/jd-status \
  | python3 -m json.tool | grep -c '"stage": "linkgrabber"'
```
Many `linkgrabber`-stage packages + healthy `jd_poll` + static Downloads = the `ASK` dialog.

---

## 3. Remediation

**For Hypothesis A** — both retry paths explicitly clear the blocking reason code
(`download_queue.py:2286-2291` and `2366-2372`), setting `last_reason_code = NULL` and state
`ready`, which makes items claimable again:

- `POST /download/retries/{item_uuid}/retry` — one item
- `POST /download/retries/retry-ready` — bulk, takes an interval

These routes require auth (only `/health` is open). Use the browser session or a token.

⚠️ **Before bulk-retrying, check JDownloader for already-delivered packages.** The whole reason
these rows are excluded is that their delivery outcome is unknown; a blind bulk retry can
duplicate grabs that already succeeded.

**For Hypothesis B** — in JDownloader: **Settings → LinkGrabber**, set the added-dupes and
added-offline actions to `EXCLUDE` / `EXCLUDE_OFFLINE`. That means *skip and leave in the Links
menu*, which is what the operator asked for. **Do not** use the `_AND_REMOVE` variants — those
delete the links instead of leaving them visible.

---

## 4. The JD dead/duplicate-link investigation (closed, with a refuted commit)

### Commit `73789ea` — "stop dead and duplicate links holding up the download queue"

Pushed to `claude/scanhound-changes-review-4wmh7n`. **No PR was opened. Its diagnosis was
refuted. Recommend reverting it.**

It added two guards to the `api` transport in `send_to_jdownloader`:
- `_filter_known_links()` — drop links already in the LinkGrabber
- `_release_online_links()` — after link check, move non-offline children to the download list

Tests: 21 new, full suite green (5457 passed, 4 skipped).

### Why it is wrong

Claimed premise: `add_links(autostart=True)` hands the whole package — including OFFLINE
children — to auto-confirm, so one dead mirror holds the package.

Codex rejected this. Both of its claims were then **independently verified against JDownloader's
actual source** (`github.com/mirror/jdownloader`, master):

| Finding | Evidence | Status |
|---|---|---|
| **M1** — JD already excludes offline children | `AutoStartManager.java`: `if (child.getLinkState() == AvailableLinkState.OFFLINE) { createNewSelection = true; continue; }` | **CONFIRMED** — the release pass reimplements what JD does natively |
| **M2** — dupe test is against the *download list*, not the LinkGrabber | `ConfirmLinksContextAction.java`: `} else if (!DownloadController.getInstance().hasDownloadLinkByID(id)) { continue; }` | **CONFIRMED, worse than stated** — the filter was narrowed to LinkGrabber-only mid-session, targeting exactly the wrong population |
| **M3** — affected transport never established; repo default is `folder` | `config.py:474` `"jd_method": "folder"`; guards live under `elif jd_method == "api":` | **OPEN** — resolve via `/health` `jd_enabled` |

### The real mechanism (found while verifying)

`ConfirmLinksContextAction` reads two configurable settings:

- `CFG_LINKGRABBER.CFG.getDefaultOnAddedDupesLinksAction()` → `INCLUDE | EXCLUDE | EXCLUDE_AND_REMOVE | ASK | GLOBAL`
- `CFG_LINKGRABBER.CFG.getDefaultOnAddedOfflineLinksAction()` → `INCLUDE_OFFLINE | EXCLUDE_OFFLINE | EXCLUDE_OFFLINE_AND_REMOVE | ASK | GLOBAL`

`ASK` opens a modal dialog. **The fix is JD configuration, not ScanHound code** — and it works
regardless of transport, which also dissolves M3.

Also verified: `myjdapi` 1.1.10 `AddLinksQuery` has **no `autoConfirm` field** (only `autostart,
links, packageName, extractPassword, priority, downloadPassword, destinationFolder,
overwritePackagizerRules`). An earlier proposed fix that added `autoConfirm` to the API payload
was discarded for this reason — it would have been a silent no-op.

### Suggested follow-up (not implemented)

Have ScanHound **detect** `ASK` on connect and warn, rather than silently overriding operator
JD preferences.

---

## 5. Working agreements

- **Branch:** `claude/scanhound-changes-review-4wmh7n`. Level with `origin/main` as of `73789ea`.
  PR #2 from this branch was merged earlier; treat further work as fresh changes.
- **`CLAUDE.md` mandates a Codex adversarial review** for non-trivial changes. Codex is an
  independent reviewer — ask it to *challenge*, not confirm. Preserve finding IDs (M1, M2…)
  across rounds; never silently drop one. Do **not** claim Codex reviewed something unless it
  actually did.
- **Commit attribution:** end messages with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and the `Claude-Session:` line.
- **Test deps** are hand-curated in `.github/workflows/tests.yml`; a local venv needs at minimum
  `pytest pytest-asyncio requests aiohttp cloudscraper beautifulsoup4 thefuzz rapidfuzz
  python-dotenv packaging selenium PlexAPI fastapi uvicorn httpx bcrypt websockets plyer myjdapi`.
- **Careful with `inspect.getsource` tests.** `tests/test_source_progress_contract.py` asserts on
  the *source text* of `download_item`. Editing `backend/download_service.py` while a suite is
  running shifts line numbers and produces 5 spurious failures. This happened once and was
  misdiagnosed as a regression before being root-caused.

## 6. Open questions

1. What is the live `jd_method` — `folder` or `api`? (`/health` → `jd_enabled`)
2. How many rows carry `interrupted_unknown_outcome`? (Hypothesis A)
3. What are JD's added-dupes / added-offline actions currently set to?
4. Did `./data` survive the PC move with the SQLite DB intact?
5. Keep or revert `73789ea`?

## 7. Do not

- Do not build on `73789ea` — its causal model is refuted.
- Do not bulk-retry interrupted items before checking JD for already-delivered packages.
- Do not use the `_AND_REMOVE` JD actions; the operator wants dead/dupe links **kept** in the
  Links menu.
