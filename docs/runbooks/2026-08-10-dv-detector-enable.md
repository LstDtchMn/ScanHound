# Runbook — turning the DV host detector ON

**Status:** DRAFT for Jesse. The DV post-rows code is **deployed** on prod (`main` @ `ad54e6a`,
image `496dfae5`) but the host detector is **not scheduled and cannot authenticate yet**. This
runbook is the checklist to close the remaining gates and switch it on. Nothing here is
destructive; the enable step (scheduling) is Jesse's.

## What is already true (deployed, verified 2026-08-10)

- `POST /rename/dv-host-rows` is registered and live. It returns **HTTP 401** — the route works,
  it is just auth-gated.
- The container migrated `crawler.db` to schema **v9**; `dv_scan` upserts now fail loudly (a
  failed write can no longer report success — round-4 fix).
- The detector (`scripts/host-detector/dv_host_scan.py`) reads its own `dv_host.db` on the host
  and POSTs rows in the request body — no bind-mount read. It sends `schema_version: 1`.

## Gate 1 — AUTH (this is the real blocker, and it needs your decision)

**Why 401:** the app runs with `--no-auth`, which only disables the *desktop nonce*. It does **not**
disable the password gate. A password is set on prod (that is why the route 401s), so every
`/rename` request must carry `Authorization: Bearer <token>`, where the token is a **login-session
token** minted by `POST /auth/login` with the admin password. The detector currently sends **no**
auth header, so it is rejected.

**The detector needs a code change** (small) plus a **credential you supply** (I will not reuse any
password/token I find on disk). The change: obtain a token and send it on every POST; on a 401,
re-authenticate once and retry. Where the token comes from is the decision:

| Option | How it works | Trade-off |
|---|---|---|
| **A. Pre-minted token (recommended)** | You log in once (`POST /auth/login`), get a long-lived session token, store it on the host in a file the scheduled task reads into `SCANHOUND_API_TOKEN`. Detector sends it as Bearer; on 401 it logs the token as expired and exits non-zero (no password on the host). | Simplest + no password stored on the host. Token expires eventually (`session_expiry()`) → you re-mint. No new app code beyond the detector. |
| **B. Password in env** | Store the admin password on the host as `SCANHOUND_API_PASSWORD`; the detector logs in each run to mint a fresh token, then POSTs. | Self-renewing (never expires), but the **admin password sits on the host** in whatever the scheduled task can read. Larger blast radius if the host is compromised. |
| **C. Dedicated service token (new app feature)** | Add a non-expiring, revocable service API key to the app (new table + `/auth` support), scoped ideally to `/rename` only. | Cleanest long-term and least privilege, but it is **new backend code + its own review round** — more work now. |
| **D. Open the app** (`SCANHOUND_ALLOW_OPEN=1`) | Removes the gate entirely. | **Not recommended** — the app is reachable via `scanhound.turtleland.us`; this drops auth for everyone. |

**My recommendation: A** for now (fastest, no password on disk, no new attack surface), with **C**
as the eventual clean answer if the detector becomes long-lived infrastructure. Once you pick, I
implement the detector change (add `--api-token`/`SCANHOUND_API_TOKEN`, Bearer header, 401 handling)
with paired tests and mutation-verify it, same discipline as the rest of this work.

## Gate 2 — exact-sentinel canary (prove VALUES land, not just counts)

Count equality proves cardinality, not correctness. Before scheduling, push **one known sentinel
row** through the live endpoint and confirm its exact values arrive in prod `crawler.db.dv_scan`.

Run on the host once auth (Gate 1) is wired, against the prod app:

```bash
# 1. Push one sentinel row (with the Bearer token from Gate 1).
curl -s -X POST https://scanhound.turtleland.us/rename/dv-host-rows \
  -H "Authorization: Bearer $SCANHOUND_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"schema_version":1,"source_rows":1,"rows":[{"path":"__CANARY__/dv-enable-check.mkv","dv_layer":"fel","sig_mtime":1.0,"sig_size":42,"title":"DV enable canary"}]}'
# Expect: {"ok":true,"source_rows":1,"processed":1,"imported":1,"updated":0,"failed":0}
```

```bash
# 2. Confirm the EXACT values persisted in prod crawler.db (run in the container).
docker exec scanhound python -c "import sqlite3;c=sqlite3.connect('/dbvol/crawler.db');r=c.execute(\"SELECT path,dv_layer,sig_mtime,sig_size,title,source FROM dv_scan WHERE path='__CANARY__/dv-enable-check.mkv'\").fetchone();print('PERSISTED:',r);assert r==('__CANARY__/dv-enable-check.mkv','fel',1.0,42,'DV enable canary','scan'),'VALUES MISMATCH';print('CANARY OK')"
```

```bash
# 3. Remove the sentinel so it never pollutes real data.
docker exec scanhound python -c "import sqlite3;c=sqlite3.connect('/dbvol/crawler.db');c.execute(\"DELETE FROM dv_scan WHERE path='__CANARY__/dv-enable-check.mkv'\");c.commit();print('canary removed, rows deleted=',c.total_changes)"
```

Only proceed if step 2 prints `CANARY OK`. A 401 here means Gate 1 is not actually solved.

## Gate 3 — PT6H runtime guard

The detector self-stops between files once `--max-runtime-minutes` (default **330** = 5h30m) is
used, which sits under a **PT6H** Windows Task Scheduler limit — the design that stops a hard-kill
from losing the final row POST. Before scheduling, confirm:

- the scheduled task's *Stop the task if it runs longer than* is **PT6H** (6 hours), and
- the detector is invoked **without** overriding `--max-runtime-minutes` above ~330 (leave the
  default, or set it explicitly to `330`).

If the schedule interval is shorter than a full run (e.g. every 6h) and a run legitimately needs
longer, use `--mode steady` for routine passes (only new/changed files, no retry sweep) so a run
finishes well inside the budget; reserve `--mode backfill` for occasional full sweeps.

## Gate 4 — scheduling (Jesse-only)

Register a Windows Scheduled Task that runs the detector periodically. Claude cannot register a
`RunLevel=Highest` task, so this step is yours. Key points from prior DV work:

- Run it from a **persistent path** — `Y:` has been a *per-session mapped* drive, which a Task
  Scheduler task will not see. Use a fixed path for both the detector and its `--db`/scan roots.
- Give the task the privilege it needs to read the library roots (elevation if the roots require
  it).
- Invocation shape (fill in real paths + the auth env from Gate 1):
  ```
  python <repo>\scripts\host-detector\dv_host_scan.py \
    --config <persistent>\dv_host.json --db <persistent>\dv_host.db \
    --api https://scanhound.turtleland.us --mode steady --max-runtime-minutes 330
  ```
- Set the task's *execution time limit* to PT6H (Gate 3).
- Do a first **supervised** run and watch the container logs for the row POST + a non-401 result
  before trusting the schedule.

## Rollback / safety

- Nothing here changes prod behaviour until Gate 4 (scheduling) runs — the detector is inert until
  invoked.
- Pre-deploy DB backup already exists: `/dbvol/crawler.db.pre-deploy-20260810` (schema v8).
- The canary row is namespaced `__CANARY__/…` and deleted in Gate 2 step 3; it never touches real
  library paths.

## Order of operations

1. **You pick the auth option (Gate 1).**
2. I implement + test the detector auth change; push for a quick review.
3. Run the canary (Gate 2) against prod → must print `CANARY OK`.
4. Confirm the PT6H guard (Gate 3).
5. You register the scheduled task (Gate 4) and do one supervised run.
