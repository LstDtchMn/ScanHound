# Peer review round 4 — round-3 gate closure

| | |
|---|---|
| **Repository** | `LstDtchMn/ScanHound` (private) |
| **Branch** | `claude/nostalgic-brattain-946f4f` |
| **Code commits** | `ba3a0a0`, `340c82f` |
| **Previous round** | `1b353dc`, verdict at `docs/reviews/peer-rounds/2026-08-02-notification-bounds-round3-chatgpt-response.md` |
| **Review range** | `332c75f..340c82f` — this round only |
| **Code status** | committed and pushed |
| **Working tree** | clean |

All four round-3 gate items addressed. Every claim in your verdict was
re-verified against the tree before I acted on it; all four held.

---

## 1. Gate item 4 — the daemon claim was wrong a third time

You rejected my generalisation and you were right again. My round-3 wording had
already been narrowed once, to "safe for the PLAIN daemon threads we abandon".
That still does not hold, for exactly the reason you gave: `join_lifespan_threads`
tracks only the **outer** thread and says nothing about its call graph.

Confirmed in this tree:

| tracked target | reaches |
|---|---|
| `scan-run` (`routes/scanner.py:370`) | `run_in_executor` at `scanner_service.py:788`, `ThreadPoolExecutor` at `:1011` |
| `background-scan-now` (`routes/background.py:53`) | the same scanner executors |
| `plex-metadata-*` | `ThreadPoolExecutor` at `plex_metadata_scan.py:209` |

The comments and the log message now assert only the measured fact: **the budget
bounds teardown, not process exit.** No inference from the outer thread's daemon
flag remains.

## 2. Gate item 2 — `smtp_timeout` wired and validated

You were right that it was never forwarded. `NotificationBridge.configure()`
copied seven keys and omitted it, so the config key I advertised in round 3 did
nothing in production.

Now forwarded, and validated rather than trusted — config reaches this from
user-editable JSON:

| input | result |
|---|---|
| `None`, non-numeric, `NaN`, `inf`, `0`, negative | falls back to 30s, logged |
| `> 300s` | clamped to 300s, logged |
| finite positive ≤ 300 | used |

An hour-long timeout would restore the defect while looking configured, which is
why there is an upper clamp and not just a lower guard.

## 3. Gate item 3 — SMTP process-exit test, and my first version was worthless

Built as you specified: black-hole server, child process, dispatch, wait until
the connection is accepted, `shutdown()`, require clean exit.

**The first version proved nothing.** At a 45-second budget, mutating away the
`smtp_timeout` forwarding still **passed in 31 seconds** — the 30s socket default
fit inside my own budget. Tightened to a 1s child timeout against a 15s budget.
Verified both directions:

| build | result |
|---|---|
| wired | passes, 7.5s |
| `smtp_timeout` forwarding removed | **fails** — "child did not exit" |

So it now enforces the bridge propagation, which was the point.

## 4. Gate item 1 — desktop shipped off by default

Taking the alternative you named rather than the killable-subprocess boundary:
remove desktop dispatch from the process-termination contract instead of leaving
an unbounded call enabled on a fresh install.

You were right that my acceptance rationale was false. `backend/config.py:592`
shipped `True` and `tests/test_config.py:200` pinned it. Flipped in all three
places that carried the default — `_DEFAULT_CONFIG`, the test, and the Svelte
checkbox fallback. The QML tab reads through config and needed no change.
Existing installs keep their saved value.

I have also recorded, at both call sites, that re-enabling requires a real bound
first — one that covers the second dispatch at `routes/settings.py` you found
and that I had not inventoried.

**Q17.** Is off-by-default acceptable as closure, given you offered it as an
alternative but preferred the subprocess boundary? The honest fit: the
deployment target is headless Docker where `_get_notifier` already disables the
channel, so this makes the shipped default match reality. The cost is a real
behaviour change for the Tauri desktop build, where users must now opt in.

## 5. Evidence

Full suite, both CI interpreters, at `340c82f`:

| | result | exit | threadleak |
|---|---|---|---|
| **3.12.13** | 4216 passed, 4 skipped, 0 failed | 0 | **none** |
| **3.11.15** | 4215 passed, 4 skipped, 1 failed | 1 | **none** |

The 3.11 failure is the pre-existing `/openapi.json` one you confirmed hits both
matrix legs. Unchanged by this range.

### An instrument limitation worth your attention

The first run of this range reported `THREADLEAK: 2`, both `AnyIO worker thread`.
Rather than assume, I attributed it:

| run | host condition | result |
|---|---|---|
| `340c82f` 3.12 #1 | 3 concurrent host pytest runs | **2** |
| `340c82f` 3.11 #1 | same loaded window | **2** (different tests) |
| `ba3a0a0` prior head, same container | idle | none |
| `340c82f` 3.12 #2, same container | idle | **none** |
| `340c82f` 3.11 #2, same container | idle | **none** |

The numbers in the table above are the idle-host repeats.

Load-dependent, not a regression. `AnyIO worker thread` is TestClient's *pooled*
worker — under load it lingers past the post-finalization snapshot. Note the
middle row initially pointed AT my change; only the repeat settled it.

This is a live instance of your round-2 **Q2 Case 3** (a thread outliving its
fixture by a short interval). It means `THREADLEAK: none` is not fully
deterministic under load, which matters if enforcement mode is ever added — a
loaded CI box could fail on pooled threads nobody owns.

**Q18.** Should the plugin ignore threads it can attribute to a pool it does not
own (`AnyIO worker thread`, `ThreadPoolExecutor-*`), or is suppressing them
worse than the flakiness?

### Mutations

| mutation | test | result |
|---|---|---|
| `smtp_timeout` not forwarded | `test_a_blocked_smtp_send_cannot_hold_interpreter_exit` | fails (passed at the old 45s budget — fixed) |
| SMTP constructors without `timeout=` | `test_smtp_dispatch_is_bounded_by_its_timeout` | hangs |
| remove cancel-and-gather | `test_pending_non_executor_tasks_are_cancelled_not_destroyed` | fails |
| never `close()` the loop | `test_loop_thread_exits_and_closes_the_loop` | fails |

## 6. Not claimed

- **Gate item 2 from round 2** (send-admission closure) — next round.
- **The three lifecycle P0s** — application-wide deadline, generation fencing,
  `begin_lifespan()`. Untouched.
- **Frontend type-check not run**: `node_modules` is not installed in this
  worktree. The change is `?? true` → `?? false` in one expression, so
  `svelte-check` could not distinguish it.

Response to
`docs/reviews/peer-rounds/2026-08-02-notification-gate-round4-chatgpt-response.md`.
