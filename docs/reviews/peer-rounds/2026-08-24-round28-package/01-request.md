# Round 28 — what I am asking you to review

Deliberately narrow. Round 27 raised six findings and I confirmed all six by
reproduction — none were refuted. So this is not a request to re-examine those
conclusions. It is a request to attack **the code I wrote in response**, which is
new, unreviewed, and sits on a data-loss path.

Same standing arrangement: equal peers, disagreement expected, neither of us the
authority. Merging, deploying, pushing and enabling are Jesse's alone.

---

## 0. Round-27 dispositions, so finding identity is preserved

| Finding | State | Where |
|---|---|---|
| **R25-1a** rename while a connection is open | fixed — the happy-path fixture no longer holds a reader open across the rename; it uses an abrupt child exit | `03-evidence.md` §3 |
| **R25-1b** close failure swallowed before rename | fixed — the close is a precondition and refuses | §3 |
| **R25-1c** refusal does not survive a restart | fixed — durable interlock checked before `sqlite3.connect()` | §2 |
| **R25-1d** notification claims success before it exists | fixed — three phases | §3 |
| **Regression G** fail-soft read = false clean | fixed — strict read, **and wired to `/health`** | §4 |
| **R26-1** §11 blast radius not behaviourally valid | **you were right; §11 is retracted and replaced with a measured matrix** | §1 |
| **R26-2** unsafe default | fixed — keyword-only and required | §5 |
| **R21-10** eighth overstated A | fixed | §6 |
| **R26-3** busy-timeout causal claim | **you were right; retracted** | §5 |

One partial disagreement, in §5 below.

---

## 1. The interlock — the thing I most want broken

`_write_quarantine_pending()` / `_refuse_if_quarantine_pending()` /
`_clear_quarantine_pending()`. This is the riskiest code in the change because it
fails in **two** directions:

- too permissive → a restart rebuilds over a stranded journal, which is the
  R25-1c data loss;
- too strict → ScanHound will not start, and recovery requires deleting a file
  by hand.

Specific attacks I want:

- **Can it wedge a healthy start?** I have a control for the ordinary case, but
  the interlock is a bare file existence check. A stale marker from a crash
  during `_write_quarantine_pending` itself, or a half-written JSON, or a
  filesystem that reports the file present after a failed delete — do any of
  those brick startup?
- **Is "before the first rename" early enough?** I chose that point because
  everything after it is destructive. But `_notify_corruption` and the logging
  run before it. Is there any state change earlier in that method that also
  needs guarding?
- **`_clear_quarantine_pending()` currently logs and continues if the removal
  fails.** So a completed quarantine whose marker cannot be deleted leaves a
  database that is fine and a process that will refuse to start next time. I
  chose fail-safe-toward-refusal deliberately, but I am not confident. The
  alternative — treat a removal failure as fatal immediately, so it surfaces
  while an operator is already looking — may be better. Argue it.
- **The marker lives beside the database.** On this deployment that is a bind
  mount whose syscall behaviour has bitten us before. Is a sibling file the
  wrong place?

## 2. The strict read, and whether `/health` is the right consumer

`incomplete_quarantine_audits()` now raises instead of returning `[]`, and
`/health` reports counts with a read failure becoming `null`.

- `/health` is **unauthenticated** — the apex host already leaks a service map,
  which is a known open item. I report counts only, and a test asserts no
  migration id or legacy key appears. Is a count itself too much? It tells an
  unauthenticated caller that this instance has damaged historical evidence.
- Your own guidance was that a caller may translate exception → unknown but
  never → clean. `/health` does exactly that, but it uses the pre-existing house
  pattern of setting the key to `None`. **Can a watcher distinguish `null` from
  the key being absent?** Absent currently means "this build has no such
  report". Those are different facts sharing one representation — which is the
  same defect class, one level up. I think this is a real remaining hole and I
  did not fix it, because changing the convention touches `arm_revisions` and
  `jd_poll` too.

## 3. The fixture change (R25-1a)

The happy path now leaves a hot WAL via a child process that calls `os._exit(0)`
after committing, rather than holding a reader open across the rename.

- Is that a faithful stand-in for the production crash state?
- I also found that constructing a `DatabaseManager` on a hot-WAL database
  checkpoints the log away, which destroyed my first attempt at the fixture. The
  test now orders it so quarantine runs without an intervening successful open —
  matching production, where quarantine happens *because* the open failed. Is
  that ordering argument sound, or am I now testing something that cannot occur?

## 4. R26-1 — I want the replacement checked, not the finding

§11 is retracted. The measured matrix is `evidence-01`:

```
_query (default=[])              RETURNED  []
_query_dicts (default=[])        RETURNED  []
_mutate                          RETURNED  False
_insert_returning_id             RETURNED  None
incomplete_quarantine_audits     RETURNED  []      <- before the round-27 fix
load_plex_cache (fail-soft)      RETURNED  []
list_plex_cache_movies_strict    RAISED    OperationalError
```

I accept your conclusion that this does not prove 30 regressions. What I have
**not** done is decide whether each fail-soft boundary is correct — you named
`/scan-history` as one that should keep degrading and I agree. The rest are
unreviewed by either of us.

## 5. Where I partly disagree

**The §12 test.** You said the `assert not issubclass(QuarantineIncomplete,
OSError)` assertion "may remain a harmless type-design preference, but it is not
the safety proof", and asked me to rewrite it so the evidence record does not
preserve a superseded causal model. I rewrote the docstring to say exactly that
— but I **kept** the assertion and added
`test_the_handler_re_raise_is_what_actually_propagates`, which asserts against
the source that the `OSError` handler terminates in a raise.

My reasoning: a behavioural test cannot distinguish "the guard raised a type the
handler does not catch" from "the handler re-raised", because both produce
`QuarantineIncomplete` at the caller. Only the source assertion pins the property
your mutation identified. If you think a source-inspecting test is the wrong
instrument here, say so — I am aware it breaks on refactor.

## 6. Not asking

- Not asking whether to merge, push, deploy or enable.
- Not asking for the AST lint or the mutation policy from your round-27
  recommendations. Both are worth building and neither is in this change; I did
  not want to bundle a new tool with the fixes it would have found.
