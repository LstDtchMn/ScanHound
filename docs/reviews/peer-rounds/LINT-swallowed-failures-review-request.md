# The swallowed-failure checker — for the next review round

**Commit:** `2e3247e` (held local at Jesse's direction; not pushed)
**Files:** `scripts/lint_swallowed_failures.py`,
`tests/test_lint_swallowed_failures.py`, 12 annotations in `backend/`,
one CI step.

This is the Layer-1/Layer-2 static check from your round-27 recommendation. It
is deliberately **not** in the round-28 package — bundling a new tool with the
fixes it would have found makes both harder to judge, and the tool has its own
design decisions that deserve their own argument.

Every judgement below is one I made alone.

---

## What it does, briefly

Two rules, stdlib `ast` only, no dependencies:

- **`inert-guard`** — a `raise` caught by a handler in its own `try`, where that
  handler produces no failure signal. Gates CI.
- **`swallowed-at-boundary`** — a broad handler (`Exception`, `OSError`,
  `sqlite3.Error`, …) inside a safety-critical function that absorbs and
  continues. Gates CI.
- **`guard-reaches-own-handler`** — same as the first, but the handler returns a
  value, so it is probably a deliberate typed-failure conversion. Advisory,
  does **not** gate.

Exception hierarchy is resolved properly, including classes defined in the tree:
`except OSError` catches `PermissionError`, `except sqlite3.Error` catches
`OperationalError`. Name matching would have missed the round-26 defect.

**Acceptance:** the fixtures are the real code as it shipped, paired with the
fixed version. Both defects are reported; both fixes are clean.

---

## 1. The heuristic I am least happy with

Layer 2 decides what is "safety-critical" **by function name**:

```python
CRITICAL_NAME_PARTS = ("quarantine", "recover", "migrat", "authority",
                       "revoke", "integrity", "attest", "corrupt")
```

This is where both real defects lived, so it is not arbitrary — but it is
obviously fragile:

- It misses critical code with an ordinary name. `record_listing_claims` is the
  live attribution writer and matches nothing in that list.
- It breaks silently on rename. A function moved out of the list stops being
  checked and **nothing reports that it stopped**, which is itself the failure
  mode this whole tool is about.
- It over-matches: `migrat` catches every one-time JSON import.

Alternatives I considered and rejected, and I would like this argued:

- **A decorator** (`@failure_must_propagate`), which you suggested. Explicit and
  rename-proof, but it only protects functions someone remembered to decorate —
  and the round-27 defect was in a function nobody thought to look at.
- **An explicit path/function allowlist in a config file.** Same problem, plus it
  drifts.
- **Flag every broad handler everywhere.** Honest, and it would be switched off
  in a week. `backend/` has hundreds.

I do not think name matching is right. I think it is the least-wrong thing I
could build in one pass, and I would rather have a better idea than defend it.

## 2. The suppression mechanism has no teeth

```python
except Exception:  # fail-soft-ok: bonus channel, the ERROR log is primary
```

The reason is **required** — a bare marker is itself reported — but nothing
checks the reason is *true*. A sufficiently bored person writes
`# fail-soft-ok: fine` and the check is silenced forever.

I think the required-reason rule is still worth having, because it converts a
silent decision into a written one that shows up in review. But it is a social
control, not a technical one, and I want to be clear that I know the difference.

`--list-suppressions` prints all 12 with their reasons, so they can be audited
as a set rather than discovered one at a time. That is the only real defence.

## 3. The 12 annotations are unreviewed

To make the tool adoptable I triaged every boundary it reported in `backend/`
and annotated each with a reason drawn from the code's own comments. **I wrote
all 12 and nobody has checked any of them.** They are the tool's entire
suppression surface, so if one is wrong the check has a permanent blind spot at
exactly the place someone already decided was risky.

Run `python scripts/lint_swallowed_failures.py --list-suppressions backend/`.
The two most load-bearing:

```
database.py:2459  the raise above converges here on purpose: this handler
                  QUARANTINES and rebuilds, which is the recovery, not an
                  absorption
database.py:2722  the quarantine already SUCCEEDED; an unremovable interlock
                  fails toward refusing the next start, which is the safe
                  direction
```

The second is the `_clear_quarantine_pending` decision I flagged in the round-28
request as one I am not confident about. The lint flagged it independently,
which I take as mild evidence the instinct is right and the resolution is still
open.

## 4. Two refinements the real codebase forced, both worth attacking

**The defect/advisory split.** Running it immediately produced a false-positive
class I had not anticipated: raise-to-converge-on-your-own-handler, which
`backend/rename/service.py:1703` documents doing on purpose. I split on "does
the handler produce a failure signal", where signal means *returns a value*.

That is crude. A handler that returns `{"ok": False}` and one that returns `[]`
are indistinguishable to it. I chose to demote rather than suppress, so these
still appear — but if the advisory tier is noise, it will be ignored, and an
ignored tier is worse than no tier.

**`_failure_is_guaranteed_after`.** It flagged `_refuse_if_quarantine_pending`,
written the same day, where a handler absorbs a failed read of an *optional*
detail and the function raises unconditionally two lines later. So it now skips
a swallow when the enclosing function raises afterwards.

That check only looks at **direct siblings** of the `try` in the function body. A
raise nested inside a later `if` is conditional and correctly does not count —
but a raise inside a later `with` block, or a helper that always raises, is
missed and will produce a false positive. Conservative in the right direction, I
think, but say if not.

## 5. What it cannot do, and what I would build next

It is a local AST pass. It cannot prove the interprocedural exception set:

```python
try:
    move_bundle()          # gains a new OSError six months from now
except OSError:
    ...
```

Nothing in the calling file knows what `move_bundle()` raises. This is one half
of your recommendation; the other half — mutation of every terminal recovery
re-raise, and fault-injection matrices at public boundaries — is not built.

I would rather hear whether the static half is worth keeping before building the
dynamic half on top of it.

## 6. The honest limit of the acceptance test

It validates against the two defects I already knew about. That is the strongest
test available to me and it is still backward-looking. Whether it catches a
**novel** instance is unproven until one appears, and a checker that only ever
catches its own training set is a very expensive regression test.

The one piece of forward evidence: it flagged code I had written that day and I
did not know was wrong. That is not nothing, but it is one data point.
