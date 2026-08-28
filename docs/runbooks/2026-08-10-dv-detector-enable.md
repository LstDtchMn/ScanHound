# Runbook — turning the DV host detector ON

> **Status (2026-08-27): EVERY GATE BELOW IS DONE. This merges as a historical
> record, not as instructions.** The ingest key is live in the container
> environment (measured), `dv_scan` holds 13,000+ rows and grows daily, the
> scheduled task runs, and the hourly auto-sync applies labels (its log line
> now reports removals too). The two lines below that date this document —
> "the running container ... is still `ad54e6a`" and the deploy target
> `9227578` — describe 2026-08-11; the container has been redeployed many
> times since (2026-08-26: image `78324087070f`, main `7db8621`).
> `dv_file_tagging` remains `false` by design. Nothing in this runbook is a
> gate for anything not yet done.


**Status (2026-08-11):** **Gate 1 (auth) is DONE and MERGED to `main` (`9227578`, PR #60)** — a
least-privilege ingest key scoped to exactly `POST /rename/dv-host-rows`, through two security-review
rounds (redirect leak + ambient-proxy leak both closed, mutation-verified). What remains is
operational: **the running container predates the merge (it is still `ad54e6a`)**, so activating the
key needs a redeploy, then the canary and the scheduled task. The enable step (scheduling) is Jesse's.
Nothing here is destructive.

## DEPLOY CHECKLIST — execution order (copy-paste)

Do these in order. Steps 1-3 activate the key; 4 is the safety canary; 5-6 turn the detector on.

**1. Generate the secret + its hash** (keep the SECRET private; only the HASH goes on the server):
```bash
python -c "import secrets,hashlib; s=secrets.token_urlsafe(32); print('SECRET (host):',s); print('HASH   (server):',hashlib.sha256(s.encode()).hexdigest())"
```

**2. Configure the server hash.** In `X:\Docker Apps\ScanHound\docker-compose.yml`, under the
`scanhound` service's `environment:` block, add (best kept in an untracked `.env` beside the compose
file, since even the hash is better off out of git):
```yaml
      - SCANHOUND_DV_INGEST_KEY_SHA256=<HASH from step 1>
```

**3. Redeploy to main so the ingest-key code goes live** (from `X:\Docker Apps\ScanHound`, on `main`
`9227578`). This is behavior-neutral for the running app — no schema change, no DV detector yet:
```bash
git checkout main && git pull --ff-only && docker compose up -d --build
```
Then confirm the key is accepted (expect `ok:true`, not 401) — this IS Gate 2 step 1 below.

**4. Run the exact-sentinel canary (Gate 2)** over loopback with the real header — prove one row's
VALUES land in `crawler.db`. See Gate 2 for the full prove-absent / assert-response / assert-values /
assert-deletion sequence. The auth header is:
```
X-DV-Ingest-Key: <SECRET from step 1>
```

**5. Configure the detector** on the host: put the SECRET in the scheduled task's environment as
`SCANHOUND_DV_INGEST_KEY` (an ACL-restricted `.env`/file the task identity reads — NOT the command
line), point `--api http://127.0.0.1:9721`, and set `dv_file_tagging=false` in `data/dv_host.json`
for the first run.

**6. Register the least-privilege scheduled task (Gate 4)** with `--max-runtime-minutes 300`,
`MultipleInstances=IgnoreNew`, UNC (not mapped `Y:`) media paths, and do one **supervised** run.

Details and rationale for each gate follow.

> **Peer review folded in.** Findings verified against the code before adoption: (1) a login-session
> token is a **30-day full-API** credential, not detector-scoped — auth uses a scoped ingest key
> (shipped); (2) the fixed-sentinel canary could false-green — now a unique sentinel with response +
> absence + deletion asserts; (3) the 330-min PT6H budget was too tight — now 300; (4)
> `dv_file_tagging=true` mutates media — explicit off gate; (5) least-privilege task, single-instance,
> loopback transport; (6, round-2) the credential cannot be carried by a redirect or an ambient proxy.

## What is already true (deployed, verified 2026-08-10)

- `POST /rename/dv-host-rows` is registered and live. It returns **HTTP 401** — the route works,
  it is just auth-gated.
- The container migrated `crawler.db` to schema **v9**; `dv_scan` upserts now fail loudly.
- The detector reads its own `dv_host.db` on the host and POSTs rows in the request body (no
  bind-mount read), sending `schema_version: 1`. With `dv_file_tagging=true` it also runs
  `mkvpropedit` and **modifies the MKV** — so it is not read-only in that mode.

## Gate 1 — AUTH — ✅ DONE (merged `9227578`, PR #60)

**Shipped:** the endpoint-scoped ingest key below (option C-min) was built and merged after two
security-review rounds. Server: `SCANHOUND_DV_INGEST_KEY_SHA256` (hash only); detector:
`SCANHOUND_DV_INGEST_KEY` → `X-DV-Ingest-Key`, sent via an unredirected header through an opener that
refuses redirects and ambient proxies, so the credential reaches only the configured origin. It
authorizes exactly `POST /rename/dv-host-rows` and nothing else. The rest of this section is the
decision record for why a scoped key rather than a session token.

**Why 401:** the app runs `--no-auth`, which disables only the *desktop nonce*. The password gate
is active, so every `/rename` request needs `Authorization: Bearer <token>`. `token_authorized()`
accepts a valid **session token** (or the now-empty nonce) — and it applies **no path or method
scope**: any unexpired session token authorizes the *entire* protected API. `/auth/login` issues
session tokens with a **30-day TTL** (`SESSION_TTL_DAYS = 30`).

**The blast-radius fact that drives this decision:** `/rename` includes destructive routes —
`/jobs/{id}/apply`, `/jobs/{id}/undo`, `DELETE /jobs/{id}`, `/jobs/bulk/apply`, `/jobs/bulk/delete`,
`/trash/delete`, `/trash/empty`, `/process-folder`. A session token stolen from the detector host
is therefore a 30-day key to **move and delete media files**, not merely to poison DV inventory.

| Option | How it works | Pros | Cons |
|---|---|---|---|
| **C-min. Endpoint-scoped ingest key (peer + my recommendation)** | New backend: a 256-bit random secret; server stores `SHA-256(secret)`; middleware allows it **only** for `POST /rename/dv-host-rows` (constant-time compare), zero authority on every other route. Detector sends it as a header. | **Least privilege** — a stolen key can only poison DV inventory, never move/delete files. Non-expiring, individually revocable. Right long-term answer. | New backend code + its own review round before it can ship — slower to first DV run. |
| **A. Pre-minted session token (temporary bridge)** | You mint one session (separate from the browser), store just the token on the host; detector sends it as Bearer; on 401 it logs "expired" and exits non-zero. | No new app code beyond the detector; fastest to first run. | It is a **30-day FULL-API** credential — a stolen token can move/delete files. Expires in 30 days → renewal procedure needed. Requires hardening (below). |
| **B. Password in host env** | Detector stores the admin password, logs in each run. | Self-renewing. | **Rejected** — admin password at rest on the host mints fresh full-API sessions indefinitely; largest blast radius. |
| **Cloudflare Access service token** | Machine token at the edge. | Good *second layer* if the detector must use the public hostname. | Does **not** replace ScanHound auth (origin still needs a credential); unnecessary for a same-host detector. |
| **Reverse-proxy-injected bearer / mTLS / IP allowlist** | Proxy adds a credential / cert / network gate. | — | Proxy-injection is an auth-bypass risk and is **not recommended**; mTLS is overkill same-host; IP allowlist is defense-in-depth only, not authentication. |

**Recommendation: the endpoint-scoped ingest key (C-min).** It is a small, focused change (one
secret, one middleware branch, negative-scope tests) — not a full service-token framework — and it
is the only option where a compromised detector host cannot reach the destructive routes. **A** is
an acceptable *temporary* bridge if you want DV running sooner, but only with the hardening below.

**If Option A (bridge) is chosen, required hardening:** mint a *separate* detector session (never
the browser token); document the 30-day expiry + a renewal/failure procedure; store the token
**outside the repo**, **not on the Task Scheduler command line**, in an ACL-restricted file
readable only by the task identity + SYSTEM + Administrators (optionally DPAPI-protected).

**Transport (both options):** point the detector at **`http://127.0.0.1:9721`** (host loopback),
not `scanhound.turtleland.us`. That keeps the machine credential off the public ingress path and
avoids coupling a local batch job to Cloudflare/NPM availability. App auth is still required.

Once you pick, I implement it with paired negative-scope tests and mutation-verify, then it gets
its own peer review round before deploy (per your call).

## Gate 2 — exact-sentinel canary (unique, response-checked)

Count equality proves cardinality, not correctness — and a *fixed* sentinel can false-green: if a
prior canary row was left behind and the new POST 401s, a values-only check still sees the old row.
So: **unique per-run sentinel, prove it is absent first, assert the HTTP response, assert the exact
persisted values, then assert exactly one deletion** (trap/finally so a failed assert never leaves
a stale row). Use the loopback URL and the Gate-1 credential.

```bash
# Unique sentinel per run (no Date.now on the host? use a run tag you pass in).
SENTINEL="__CANARY__/dv-enable-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM.mkv"

# 0. Precondition: prove the sentinel is ABSENT.
docker exec scanhound python -c "import sqlite3,sys;c=sqlite3.connect('/dbvol/crawler.db');n=c.execute('SELECT count(*) FROM dv_scan WHERE path=?',['$SENTINEL']).fetchone()[0];sys.exit(0 if n==0 else 1)" || { echo 'sentinel already present — abort'; exit 1; }

# 1. POST it (loopback, Gate-1 credential) and REQUIRE the exact JSON + 2xx.
resp=$(curl -s -w '\n%{http_code}' -X POST http://127.0.0.1:9721/rename/dv-host-rows \
  -H "X-DV-Ingest-Key: $SCANHOUND_DV_INGEST_KEY" -H "Content-Type: application/json" \
  -d "{\"schema_version\":1,\"source_rows\":1,\"rows\":[{\"path\":\"$SENTINEL\",\"dv_layer\":\"fel\",\"sig_mtime\":1.0,\"sig_size\":42,\"title\":\"DV enable canary\"}]}")
echo "$resp" | tail -1 | grep -qx 200 || { echo "POST not 200: $resp"; exit 1; }
echo "$resp" | head -1 | grep -q '"ok": *true' && echo "$resp" | head -1 | grep -q '"processed": *1' && echo "$resp" | head -1 | grep -q '"failed": *0' || { echo "response body failed assert: $resp"; exit 1; }

# 2. Prove the EXACT values persisted.
docker exec scanhound python -c "import sqlite3;c=sqlite3.connect('/dbvol/crawler.db');r=c.execute('SELECT path,dv_layer,sig_mtime,sig_size,title,source FROM dv_scan WHERE path=?',['$SENTINEL']).fetchone();assert r==('$SENTINEL','fel',1.0,42,'DV enable canary','scan'),('MISMATCH',r);print('VALUES OK')"

# 3. Cleanup: delete exactly one row.
docker exec scanhound python -c "import sqlite3;c=sqlite3.connect('/dbvol/crawler.db');c.execute('DELETE FROM dv_scan WHERE path=?',['$SENTINEL']);c.commit();assert c.total_changes==1,('expected 1 deletion',c.total_changes);print('CANARY OK — cleaned up')"
```

This curl canary proves the **endpoint + storage mapping**. It does not exercise the detector's own
`_post_rows()` sender — that is covered by the auth-implementation tests and the supervised first
run (Gate 4).

## Gate 3 — PT6H runtime guard (use 300, not 330)

`--max-runtime-minutes` is checked **only between files**. Once a file starts, its worst-case tail
is roughly: DV detect up to **1800 s** + optional `mkvpropedit` up to **300 s** + final POST up to
**300 s** ≈ **40 min**. So a 330-min budget under a 360-min (PT6H) hard limit leaves only ~30 min —
*less* than one file's tail. A file starting at minute 329 can blow past PT6H.

- Initial deploy: **`--max-runtime-minutes 300`** (≈60-min reserve). 
- Use `--mode steady` for routine passes (only new/changed files, no retry sweep) so runs finish
  well inside budget; reserve `--mode backfill` for occasional full sweeps.
- Mitigation already in place: interim cumulative POSTs every 25 files + the durable `dv_host.db`
  mean a hard kill is **not** catastrophic (a later cumulative POST self-heals unpublished rows) —
  but still target a clean final handoff, don't rely on recovery.
- Future hardening (code round): propagate a hard deadline and refuse to *start* an expensive stage
  when the shutdown reserve is insufficient.

## Gate 4 — scheduling (Jesse-only)

Register a Windows Scheduled Task; Claude cannot register elevated tasks. Requirements:

- **Least privilege, NOT `RunLevel=Highest` by default.** Use a dedicated task identity with only:
  read/execute on Python + detector code + `dovi_tool`; read on the media roots; read/write on the
  `dv_host.db` dir, the detector logs, and the credential file. Grant media **write** only if
  tagging is intentionally enabled.
- **Single instance: `MultipleInstances=IgnoreNew`.** Overlapping runs duplicate expensive media
  reads, share one `dv_host.db`, and (if tagging is on) could both edit MKV headers.
- **`dv_file_tagging=false` for the first enablement** (it defaults false — keep it so). With it
  true the detector mutates media via `mkvpropedit`; do not combine "first unattended run" with
  "media writes" unless that is explicitly the plan.
- **Persistent paths, verified under the TASK identity** (not your interactive shell): local
  absolute paths for script/config/DB/logs; **UNC** paths for network media (a per-session mapped
  `Y:` is invisible to Task Scheduler); the task identity needs both SMB-share and NTFS rights.
- Before unattended operation, prove under the task identity that: `python` resolves, `dovi_tool`
  resolves, the config exists, the `dv_host.db` dir is writable, all library roots are readable, the
  API endpoint is reachable, and the credential file is readable.
- Invocation shape (fill in real paths + Gate-1 auth env):
  ```
  python <repo>\scripts\host-detector\dv_host_scan.py \
    --config <persistent>\dv_host.json --db <persistent>\dv_host.db \
    --api http://127.0.0.1:9721 --mode steady --max-runtime-minutes 300
  ```
- Set the task's *execution time limit* to PT6H.

## Rollback / safety

- Nothing here changes prod behaviour until Gate 4 runs — the detector is inert until invoked.
- Pre-deploy DB backup exists: `/dbvol/crawler.db.pre-deploy-20260810` (schema v8).
- The canary row is namespaced `__CANARY__/…`, proven absent before insert and deleted after; it
  never touches real library paths.

## Order of operations (revised)

1. **You pick the auth option (Gate 1)** — endpoint-scoped ingest key (recommended) or the
   hardened pre-minted-token bridge.
2. I implement + test it (negative-scope tests for the key; sender/401 tests either way), push for
   its own peer review round.
3. Run the unique, response-checked canary (Gate 2) over **loopback** → must print `CANARY OK`.
4. Confirm the PT6H reserve (Gate 3, `--max-runtime-minutes 300`).
5. Verify `dv_file_tagging=false` unless media writes are intended.
6. You register the least-privileged, single-instance task on persistent/UNC paths (Gate 4),
   verify the environment under the task identity, and do one supervised run watching for: live
   progress, interim/final `dv-host-rows OK`, no 401, normal detector exit, a successful Task
   Scheduler result, and container DV rows advancing.
