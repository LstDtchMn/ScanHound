# ScanHound — session handoff

**Written:** 2026-09-21 · **Repo:** LstDtchMn/ScanHound
**Branch:** `claude/scanhound-changes-review-4wmh7n` (2 commits ahead of `origin/main`)
**Written by:** a cloud-sandbox session with **no route to the production host**. Everything
below is either verified from source (marked) or flagged as unverified. **Nothing here was ever
executed against the live instance.**

Start at §2 — there is a live production problem.

---

## 1. Read-first map

This repo already carries a lot of institutional memory. Do not re-derive it.

| File | What it is | Trust |
|---|---|---|
| `CLAUDE.md` | **Mandatory workflow.** Claude implements, Codex reviews adversarially. | Current |
| `CODEX_REVIEW.md` | The Codex review protocol in detail | Current |
| `AGENTS.md` | Agent conventions | Current |
| `docs/TODO.md` | Living remaining-work checklist (DV FEL/MEL feature, misc) | Last updated 2026-06-29 |
| `docs/reviews/STATE-OF-PLAY.md` | Canonical outstanding-work doc | **STALE — see §8** |
| `SCANHOUND-CLOUD-SESSIONS-CATCHUP.md` | Cross-session relay log (round-12 verdict) | Historical |
| `docs/HANDOFF.md`, `docs/HANDOFF-2026-08-17*.md` | Prior handoffs | Historical |
| `docs/UNFINISHED-WORK-2026-08-16.txt`, `docs/PRIORITY-PLAN-2026-08-16.md` | Aug-16 planning | Historical |
| `DOCKER.md`, `DEPLOY_INSTRUCTIONS.md` | Deployment | Current |
| `docs/runbooks/`, `docs/reviews/`, `docs/designs/`, `docs/specs/` | Deep history per feature | Varies |

---

## 2. PRIORITY: downloads are currently stuck

Operator report: *"It seems like a lot of downloads are currently stuck."*

**Critical context: the Docker container was just moved from one PC to another.** That single
fact reorders the hypotheses.

### Hypothesis A — interrupted claims (VERIFIED IN CODE — check this first)

Moving the container stops the process. On startup, `recover_interrupted()` takes **every** row in
state `claimed` and rewrites it — `backend/download_queue.py:276-295`:

```sql
UPDATE download_queue_items
SET state = 'failed',
    queue_reason = 'manual_retry',
    last_reason_code = 'interrupted_unknown_outcome',
    transport_attempted = 1, ...
WHERE state = 'claimed'
```

Those rows are then **permanently excluded from automatic claiming**. Verified at three sites —
`_claim_due()` at `download_queue.py:752-755` and `863-866`, auto-resume at `1935-1938`:

```sql
AND COALESCE(last_reason_code, '') NOT IN (
    'operation_timeout_unknown',
    'interrupted_unknown_outcome'
)
```

This is **deliberate, not a bug**: an interrupted item's delivery outcome is unknown, and
auto-retrying risks double-submitting a grab that already reached JDownloader. But it means a host
move strands every in-flight item until a human acts. **This fits the symptom best.**

### Hypothesis B — JD dupe/offline `ASK` dialog (VERIFIED against JDownloader source — see §5)

If JD's LinkGrabber is set to `ASK` on added dupes or offline links, it opens a **modal dialog**.
That blocks JD's GUI confirm path but **not** its API — so `jd_poll` reads healthy while packages
pile up unconfirmed in the LinkGrabber. Distinct signature from A.

### Hypothesis C — stale `jd_device` after the move (UNVERIFIED)

`_connect_jd_device()` calls `jd.get_device(device_name)` from config key `jd_device` and **raises**
if the name is not found. If JDownloader was reinstalled or renamed on the new PC, every send and
poll fails. Signature: `jd_poll.failure_phase == "get_device"`.

### Hypothesis D — host-dependent config broken by the move (UNVERIFIED)

`docker-compose.yml` maps `host.docker.internal:host-gateway` for Plex `:32400` and joins an
**external** `proxy` network. On a new host verify: the `proxy` network exists, Plex is reachable,
and `./data` came across with the SQLite DB intact.

### Hypothesis E — Plex repoint (CROSS-SESSION — see §9)

A parallel session, *"Plex library repoint adversarial review"*, was active hours before this was
written, reporting *"six hypotheses eliminated; traps set for event-centric correlation."* If Plex
library paths moved with the server, that work overlaps. **Check that session before duplicating
its investigation.**

---

## 3. Diagnostic playbook

`/health` is **deliberately unauthenticated** so a watchdog can read it without a credential.

```bash
docker exec scanhound curl -s localhost:9721/health | python3 -m json.tool
```

| Field | Meaning |
|---|---|
| `jd_enabled` | `jd_enabled AND jd_method == "api"` — **resolves which transport is live** |
| `queue.executor_starved` | Work due, nothing **started** — scheduler/ownership/liveness fault |
| `queue.source_no_progress` | Attempts running, source delivering nothing — source fault |
| `queue.human_required` | Verification hold or auto-resume exhausted — **no timer clears this** |
| `jd_poll.failure_phase` | `connect` / `update_devices` / `get_device` / `query_packages` |
| `jd_poll.stalled_seconds` | Time since JD last answered |

`jd_poll` liveness triage (`download_service.jd_poll_health`):

| Signature | Diagnosis |
|---|---|
| `cycles_started == cycles_completed`, static | Poller thread **stopped** |
| `cycles_started > cycles_completed`, `current_cycle_seconds` growing | **Blocked** mid-call |
| Both advancing, `stalled_seconds` growing | Cycling but **failing** — read `failure_phase` |

**Confirm Hypothesis A** (verify the DB path first — `HOME=/data`, so config is
`/data/.config/scanhound`, data `/data/.local/share/scanhound`):

```bash
docker exec scanhound sqlite3 /data/.local/share/scanhound/scanhound.db \
  "SELECT last_reason_code, state, COUNT(*) FROM download_queue_items
   GROUP BY last_reason_code, state ORDER BY 3 DESC;"
```

**Confirm Hypothesis B:**
```bash
docker exec scanhound curl -s localhost:9721/download/jd-status \
  | python3 -m json.tool | grep -c '"stage": "linkgrabber"'
```
Many `linkgrabber`-stage packages + healthy `jd_poll` + static Downloads = the `ASK` dialog.

---

## 4. Remediation

**Hypothesis A.** Both retry paths explicitly clear the blocking reason code
(`download_queue.py:2286-2291`, `2366-2372`) — setting `last_reason_code = NULL`, state `ready`:

- `POST /download/retries/{item_uuid}/retry` — one item
- `POST /download/retries/retry-ready` — bulk, takes an interval

These require auth (only `/health` is open). Use the browser session or a token.

> ⚠️ **Check JDownloader for already-delivered packages before bulk-retrying.** The exclusion
> exists precisely because these outcomes are unknown; a blind bulk retry can duplicate grabs that
> already succeeded.

**Hypothesis B.** In JDownloader: **Settings → LinkGrabber**, set added-dupes and added-offline
actions to `EXCLUDE` / `EXCLUDE_OFFLINE` — *skip and leave in the Links menu*, which is what the
operator asked for. **Do not** use the `_AND_REMOVE` variants; those delete the links.

---

## 5. The JD dead/duplicate-link investigation (closed — commit refuted)

### `73789ea` — "stop dead and duplicate links holding up the download queue"

Pushed, **no PR opened, diagnosis refuted. Recommend reverting.**

Added two best-effort guards to the `api` transport in `send_to_jdownloader`:
`_filter_known_links()` (drop links already in the LinkGrabber) and `_release_online_links()`
(after link check, move non-offline children to the download list). 21 new tests; full suite green
(5457 passed / 4 skipped).

### Why it is wrong

Claimed premise: `add_links(autostart=True)` hands the whole package — including OFFLINE children —
to auto-confirm, so one dead mirror holds the package. **Codex rejected this**, and both claims were
then **independently verified against JDownloader source** (`github.com/mirror/jdownloader`, master):

| ID | Finding | Evidence | Status |
|---|---|---|---|
| **M1** | JD already excludes offline children | `AutoStartManager.java`: `if (child.getLinkState() == AvailableLinkState.OFFLINE) { createNewSelection = true; continue; }` | **CONFIRMED** — the release pass reimplements what JD does natively |
| **M2** | Dupe test is against the *download list*, not the LinkGrabber | `ConfirmLinksContextAction.java`: `} else if (!DownloadController.getInstance().hasDownloadLinkByID(id)) { continue; }` | **CONFIRMED, worse than stated** — the filter was narrowed to LinkGrabber-only mid-session, hitting exactly the wrong population |
| **M3** | Affected transport never established; repo default is `folder` | `config.py:474` `"jd_method": "folder"`; guards sit under `elif jd_method == "api":` | **OPEN** — resolve via `/health` `jd_enabled` |

### The real mechanism (found while verifying M1/M2)

`ConfirmLinksContextAction` reads two configurable settings:

- `CFG_LINKGRABBER.CFG.getDefaultOnAddedDupesLinksAction()` → `INCLUDE | EXCLUDE | EXCLUDE_AND_REMOVE | ASK | GLOBAL`
- `CFG_LINKGRABBER.CFG.getDefaultOnAddedOfflineLinksAction()` → `INCLUDE_OFFLINE | EXCLUDE_OFFLINE | EXCLUDE_OFFLINE_AND_REMOVE | ASK | GLOBAL`

`ASK` opens a modal dialog. **The fix is JD configuration, not ScanHound code** — and it is
transport-independent, which also dissolves M3.

Also verified: `myjdapi` 1.1.10 `AddLinksQuery` has **no `autoConfirm` field** (only `autostart,
links, packageName, extractPassword, priority, downloadPassword, destinationFolder,
overwritePackagizerRules`). An earlier proposed fix adding `autoConfirm` to the API payload was
discarded — it would have been a silent no-op.

**Suggested follow-up (not implemented):** have ScanHound *detect* `ASK` on connect and warn,
rather than silently overriding operator JD preferences.

---

## 6. Repo & branch state

```
25f8b70  docs: add session handoff for continuing work locally   ← this file
73789ea  fix(jd): stop dead and duplicate links ...              ← REFUTED, consider revert
0a2751d  Merge pull request #59 (origin/main HEAD, 2026-08-28)
```

Working tree clean, nothing unpushed, 2 ahead / 0 behind `origin/main`.
PR #2 from this branch was merged earlier; treat further work as fresh changes.

---

## 7. Working agreements

- **`CLAUDE.md` mandates Codex adversarial review** for non-trivial changes. Ask Codex to
  *challenge*, not confirm. Preserve finding IDs (M1, M2…) across rounds; never silently drop one.
  **Never claim Codex reviewed something unless it actually did.**
- **Commit attribution:** end messages with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and the `Claude-Session:` line.
- **Deploy only via** `docker compose up -d --build`.
- **Test deps** are hand-curated in `.github/workflows/tests.yml` (they are *not* all in
  `requirements.txt`). A local venv needs at minimum: `pytest pytest-asyncio requests aiohttp
  cloudscraper beautifulsoup4 thefuzz rapidfuzz python-dotenv packaging selenium PlexAPI fastapi
  uvicorn httpx bcrypt websockets plyer myjdapi`. Full suite ≈ 11 min, 5457 tests.
- **`inspect.getsource` trap.** `tests/test_source_progress_contract.py` asserts on the *source
  text* of `download_item`. Editing `backend/download_service.py` while a suite is running shifts
  line numbers and produces 5 spurious failures. This happened once here and was briefly
  misdiagnosed as a regression. **Never edit while a suite runs.**
- **Module-global test isolation.** This codebase has repeatedly been bitten by module-level
  globals leaking across test modules (e.g. `_last_scan_items` in `api/routes/scanner.py`). Tests
  can pass isolated and fail in the full suite. Always verify with a full run.

---

## 8. Longer-term outstanding work

**`docs/reviews/STATE-OF-PLAY.md` is STALE.** It is dated 2026-08-10 and warns that `main`'s
`dv_detect.py` parser can remove a managed Plex label, directing work to
`agent/dv-detector-consolidation`. As of this writing that branch **no longer exists on origin**,
`main` moved to 2026-08-28 (PR #59, a DV-detector runbook), and `backend/rename/dv_detect.py` has
been substantially rewritten (now carries `LAYER_FEL`, profile-aware classification, a bounded
FEL-positive accelerator, `(MEL, FEL)` handling). **The consolidation appears to have landed — do
not act on that stale warning without re-verifying.**

From `docs/TODO.md` (last updated 2026-06-29), the DV FEL/MEL feature still lists open items:
seed importer, real-file accuracy validation, per-file ingest hook, Plex label write-path, Kometa
overlay, optional file tagging, and config wiring. Non-DV: TV library unset
(`auto_rename_tv_library = ""`), `G:\Downloads` not path-mapped, live-mode scan pagination
deferred. **Re-verify all of these against current `main` before trusting them.**

---

## 9. Related Claude sessions

Titles below come from session metadata; **their contents were not read** and are unverified
leads, not established fact.

| Session | Relevance |
|---|---|
| **Plex library repoint adversarial review** | **Most relevant.** Active 2026-09-21 01:20, status *"investigation complete; awaiting old-server script run"*, *"six hypotheses eliminated"*. Overlaps the server move. Artifacts: "Plex Repoint R21/R22". |
| Turtleone Server Work / Turtletwo Server Work (Old Main) / turtlelandsrvr | The server migration itself |
| Docker setup and app container updates (several) | Container/deploy history |
| Turnstile verification hold timer · "Add a verification hold a timer cannot release" | Origin of the queue's verification-hold design — relevant if `human_required` is true |
| DV scan cannot converge: 30-min timeout + retry starvation | Queue starvation precedent |
| Stream DV detector output live instead of buffering | DV live-progress work |
| Handoff documentation and session setup (several) | Prior handoff conventions |

---

## 10. Open questions

1. Live `jd_method` — `folder` or `api`? (`/health` → `jd_enabled`)
2. How many rows carry `interrupted_unknown_outcome`? (Hypothesis A)
3. JD's added-dupes / added-offline actions — is either `ASK`?
4. Did `./data` survive the move with the SQLite DB intact?
5. Keep or revert `73789ea`?
6. Did the Plex repoint work conclude, and does it bear on the stuck queue?

## 11. Do not

- Do not build on `73789ea` — its causal model is refuted.
- Do not bulk-retry interrupted items before checking JD for already-delivered packages.
- Do not use the `_AND_REMOVE` JD actions; dead/dupe links should **stay** in the Links menu.
- Do not act on `docs/reviews/STATE-OF-PLAY.md` §1 without re-verifying — it is stale.
- Do not edit source files while a test suite is running (see §7).
