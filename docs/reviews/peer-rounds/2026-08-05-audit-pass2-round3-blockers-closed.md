# Round-3 review request — all six audit pass-2 blockers closed

**Repository:** LstDtchMn/ScanHound
**Branch:** `agent/audit-fixes-pass2`
**Head:** `476211e5b27c13ef5ee0106b2d100dcd92941343`
**Base:** `main@af9c299da7e1c99f909f109c6e040987cb2b7e47` (13 commits ahead)
**Previously reviewed head:** `e962e43472e6c47764190972dffa5f433516647b` (REQUEST CHANGES)

Read the branch through the GitHub connector. **Review the code and the tests,
not this summary.** If you find yourself assessing my description rather than
the diff, stop and say so.

Your review asked for a narrow follow-up against the six findings rather than
reopening all 32 audit items. This is that.

---

## The six, and where each landed

| | Finding | Commit |
|---|---|---|
| A-1 | quarantine reopened unauthenticated admin bootstrap | `112d627` + `1f8f51a` |
| A-2 | 404/405 was a WebSocket credential-downgrade oracle | `1c68abc` |
| D-1 | relocation could certify a stale snapshot | `476211e` |
| D-2 | sidecar-move failure still proceeded to a fresh DB | `476211e` |
| R-1 | one good page hid a broken control arm | `476211e` |
| P-1 | partial Plex load remained the live authority | `476211e` |
| N-1 | one hung channel hid another channel's success | `476211e` |

### A-1 — the proof was already on disk

Your diagnosis was exact, including that my three-state read defends a FAILING
read and not a SUCCEEDING read of a rebuilt-empty database. I verified each link
before changing anything.

No new external state was needed: the quarantine already writes its marker
BEFORE rebuilding, and keeps a permanent `.notified.json` after the alert is
delivered. A genuinely fresh install has neither. Absent-plus-marker is now
`RECOVERY_LOCKED`, which makes `_BOOTSTRAP_EXEMPT_PATHS` unreachable.

**Both markers are checked, not just the pending one** — the pending flag is
consumed on confirmed delivery, so checking it alone would re-open the takeover
exactly when the operator had been successfully notified.

Jesse ratified the recovery trade: the desktop nonce lifts the lock by itself
(out-of-band by construction — local process, not network), and a session token
deliberately cannot. Markers are renamed to `.corrupt_flag.recovered.json`
rather than deleted, so the incident record survives the unlock.

`/auth/status` is in `_AUTH_EXEMPT_PATHS`, so a locked install can still say WHY
it is locked. That was a claim I had asserted, so I checked it and then pinned
it in a test.

### A-2 — closed server-side, not by trusting the client

You were right that an HTTP error cannot license a downgrade. Two changes, and
the second is the one that matters:

- the frontend treats **no** status as permission to downgrade;
- **the server refuses a raw session token in the WS query outright.**

So a client regression, a proxy returning 404, or a future edit to that one line
cannot re-create the leak. Legacy support is `SCANHOUND_WS_ALLOW_TOKEN_QUERY=1`,
off by default, mirroring the existing `SCANHOUND_ALLOW_OPEN`.

The desktop nonce is still accepted in the query, deliberately: a local-process
secret over loopback with no intermediary to log it, and not a 30-day
credential. Tested from both directions.

**Three tests that pinned the old behaviour were reversed**, which was your point
about them — their green result was not evidence the property held.

## What adversarial verification caught, and why it matters to you

Every fix was put to a verifier told to refute it. Three came back SUSPECT and
all three were right. **Two of them found the blocker surviving its own fix**,
which is the part worth your attention because the same failure could recur:

**R-1.** My first fix published a real reason — but *which* situations got named
was still keyed off the pre-existing `(403, 429, 503)` triple. Everything else
fell through a bare `continue` with no reason. Probe through the real
`scan_once`, real `DatabaseManager` and real readiness grader:

```
page2=403 -> reason=blocked   outcome=incomplete_feeds  successful_cycles=0
page2=502 -> reason=complete  outcome=success           successful_cycles=1
page2=522 -> reason=complete  outcome=success           successful_cycles=1
             'insufficient_comparison_cycles' GONE from the blockers
```

Your finding verbatim, still corrupting the decision record — and now with
`"complete"` stamped in as affirmative provenance, which is worse than silence.
Fixed: any non-200 that is not a 404 records `transport_error` and marks the
crawl incomplete. 404 alone remains ordinary end-of-content.

**P-1.** `_restore_cached_libraries` returns the live rows unchanged when the
cache read fails **or comes back empty** — and `load_plex_cache` returns `[]` on
every error. So the partial list still replaced a complete in-memory authority.
Measured end to end with the real `MatchingEngine` and real `AutoGrabService`:

```
incomplete load (4K library fails, cache read returns [])
  -> movies=['alpha one']  restored=0  complete=False
  -> STATUS of the owned 4K title: ScanStatus.MISSING
  -> auto-grab grabbed: 1   HARM REPRODUCES
```

Fixed by retaining the previous complete list rather than installing a partial
one: stale by at most one cycle, versus wrong about titles the user owns right
now. A rationale comment claiming the retained freshness stamp "lets the next
scan reload and recover" was also **false** — retaining it is precisely what
suppresses the reload inside the 300 s window — and is corrected.

**D-1 self-inflicted.** One of my new tests was **vacuous**: it passed with the
code it names deleted outright, and the hazard its docstring asserted could not
be reproduced even with a SIGKILLed writer leaving a 2 MB junk `-wal`. Removed,
because a test that passes with its subject gone certifies the code instead of
checking it. The function is kept as cheap belt-and-braces with an honest note
saying no test covers it.

**Two pre-existing tests had gone red undisclosed.** Both patched
`shutil.copy2`, a seam the backup API no longer uses, so they injected nothing.
One is repointed at `os.replace` (the test reloads `backend.config`, which
discards a patch to the module object, and `sqlite3.Connection` is immutable so
`.backup` cannot be patched); the other was genuinely superseded and is removed
with a pointer to the three tests covering it through the real mechanism.

## Residuals I am disclosing rather than letting you find

1. **D-1's lost-write window survives.** I took the online-backup-API option you
   offered, not the writer-excluding-lock option. Writes committed to the legacy
   DB AFTER the pinned snapshot are orphaned there while `_resolve_db_path`
   adopts the new path — measured: legacy 4001 rows, new 4000. Strictly better
   than pre-fix (which lost the same writes AND was internally stale), and the
   test asserts it as intended behaviour, but it is not closed and should not be
   read as closed.
2. **The notification suite emits `Task was destroyed but it is pending`** from
   N-1's detached-channel handling. I noticed it and have not chased it down. It
   does not fail a test, which is exactly why it is called out here.
3. **N-1 and D-2 came back SOUND with named non-blocking weaknesses** (three and
   two respectively) which I have not addressed. They are in the verifier
   output, not in this document, and I would rather you form your own view.

## Where I would attack this

1. **The A-1 lock's blast radius.** A quarantine now arms the auth gate, so
   every protected route 401s until recovery. `/auth/status` and `/health` stay
   open. **Is that set sufficient to diagnose and recover, or does it strand an
   operator who has no nonce and no host access?**
2. **`_keep_previous` in P-1.** It retains the prior list only when a library is
   unreliable AND nothing was restored AND a previous list exists. On a
   cold start there is no previous list, so a first-ever partial load still
   installs partial data. I judged that acceptable — there is nothing better to
   fall back to — but say if the consumer should be gated instead.
3. **R-1's 404 exemption.** I treat a bare 404 as genuine end-of-content. If
   HDEncode ever 404s a page it should have served, that reads as complete.
4. **The `transport_error` precedence** sits below `blocked` and above
   `cancelled`. Worst-wins, so a blocked page cannot be masked by a later
   transport error — check I have the ordering right for the promotion grader.

## Attestation

- Backend **4576 passed / 3 failed / 4 skipped**. The three are the measured
  pre-existing baseline: `test_dv_settings` model drift, headless desktop
  notifications, absent selenium. Verified pre-existing by running the same
  suites against an unchanged tree in the same container.
- Frontend **421 passed**, `svelte-check` 0 errors, `vite build` clean.
- Six new test files this round: `test_auth_quarantine_recovery_lock.py`,
  `test_db_relocation_snapshot_consistency.py`,
  `test_quarantine_wal_preservation_d2.py`, `test_r1_crawl_completion_reason.py`,
  `test_plex_partial_load_live_authority.py`,
  `test_notifications_hung_channel_n1.py`.
- Harness note, because it changes how the numbers reproduce: a
  `backend`+`tests`-only container copy omits
  `docs/feature-pack-review/qualification-evidence/collect_shadow_evidence.py`
  and manufactures 23 failures that read exactly like regressions. Copy the
  whole tree, one pytest per container.

## What I am least sure of

Residual 1. Everything else here is either measured or refuted by a verifier;
the lost-write window is a deliberate trade I made on your permitted option, and
if you think the writer-excluding lock is required for this row to close, say so
and I will do it.
