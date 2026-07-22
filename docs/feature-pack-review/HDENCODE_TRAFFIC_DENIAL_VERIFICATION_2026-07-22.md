# HDEncode traffic-denial design review — independent verification

**Status:** verification only, no code changed. This checks every claim in
`CLAUDE_REPLY.md` (the design review response) against the real, current
checkout at `4718d873b33eba6b4b265fb81e224a59074d77cb` — nothing here is
taken on trust from the review package's self-report.

## Verdict

Every correction and the new duplicate-writer defect are confirmed real,
against actual source, with exact file:line evidence below. One detail the
review flagged as unconfirmed — the `consecutive_failures=2` anomaly — is
now fully explained, not just corroborated. No disagreements. Ready for the
implementation package.

## Point-by-point verification

### 1. Field name: `cooldown_until`, not `until`

Confirmed. `backend/hdencode_coordinator.py`, `HDEncodeDecision`:

```python
class HDEncodeDecision:
    blocked: bool
    state: str
    reason_code: Optional[str] = None
    cooldown_until: Optional[str] = None
```

My original proposal's `decision.until` was wrong. Corrected.

### 2. `HDEncodeTrafficDenied`'s current shape

Confirmed minimal, exactly as the review assumed:

```python
class HDEncodeTrafficDenied(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
```

No `state`, `reason_code`, or `cooldown_until` fields exist today — the
proposed extension is additive, not a rename.

### 3. `SOURCE_TEMPORARILY_BLOCKED` doesn't already exist

Confirmed via grep on `backend/scrape_outcome.py` — zero matches. The
proposed code is genuinely new, not a naming collision with something
already there.

### 4. `_local_cooldown_reason` doesn't already exist

Confirmed — `backend/hdencode_coordinator.py` has `_local_cooldown_until`
(6 references) but no `_local_cooldown_reason` anywhere. Additive, as
proposed.

### 5. The three decision-driven raise sites are unchanged

Confirmed at lines 294, 350, 374 of `hdencode_coordinator.py`, all
`raise HDEncodeTrafficDenied(decision.reason_code or decision.state, ...)`
— matches what my proposal documented. (Two other raise sites at lines 41
and 46 exist for a different, unrelated invariant — proving a transport
constructor was called through the coordinator's authorization contextvar,
not a `decision.blocked` traffic classification. Neither the proposal nor
the review touches those, and they shouldn't be touched by this fix either.)

### 6. The duplicate health-writer defect — confirmed, and the mechanism nailed down

This was the review's most significant finding, and the one I hadn't
caught. Fully traced now, in the real code:

**Writer 1 — the coordinator, via `observe_challenge()`** (`hdencode_coordinator.py:449`):

```python
def observe_challenge(self, reason_code: str = "interactive_challenge") -> None:
    seconds = 60 * 60
    until = _utcnow() + timedelta(seconds=seconds)
    with self._state_lock:
        self._metrics["challenges"] += 1
        self._local_cooldown_until = until
        self._health_cache_at = 0.0
    self._persist_failure("cooldown", reason_code, seconds)
```

`_persist_failure` forwards `cooldown_seconds=seconds` (3600) into
`db.record_source_failure(..., cooldown_seconds=cooldown_seconds)`.

**Writer 2 — `record_scrape_outcome()`** (`backend/source_health.py:59`),
called from `download_item()` immediately after `scrape_links()` returns
(`download_service.py:2258-2259`):

```python
links = self.scrape_links(url, service_type, progress_callback=_cb)
diagnostic = getattr(links, "diagnostic", None)
if _source_page_kind(url) == "hdencode":
    record_scrape_outcome(self.db, "hdencode", links)
```

For an `INTERACTIVE_CHALLENGE` diagnostic, `health_state_for_diagnostic()`
maps it to `SourceHealthState.BLOCKED`, and `record_scrape_outcome()` calls:

```python
db.record_source_failure(source, state.value, diagnostic.code.value)
```

**No `cooldown_seconds` kwarg** — it defaults to `None`.

**The overwrite mechanism, confirmed at the SQL level**
(`backend/database.py:4734-4762`, `record_source_failure`):

```python
cooldown_until = None
if cooldown_seconds:
    cooldown_until = (now_dt + timedelta(seconds=...)).isoformat()
return self._mutate(
    """INSERT INTO source_health (...) VALUES (...)
       ON CONFLICT(source) DO UPDATE SET
           state = excluded.state,
           reason_code = excluded.reason_code,
           updated_at = excluded.updated_at,
           last_failure_at = excluded.last_failure_at,
           consecutive_failures = source_health.consecutive_failures + 1,
           cooldown_until = excluded.cooldown_until""",
    ...
)
```

The `ON CONFLICT` clause unconditionally sets `cooldown_until =
excluded.cooldown_until` on every call — including calls where that value
is `None`. So within one `download_item()` invocation, for one challenged
title: Writer 1 fires first (inside `scrape_links()`, which internally
calls `_log_page_diagnostics()` → `observe_challenge()`) and persists
`cooldown_until` = now+1h. Writer 2 fires immediately after, in the same
call stack, and overwrites it with `NULL`. `_active_decision()` then has no
usable `cooldown_until` and falls through to the legacy 30-minute
`"blocked"` branch — which is exactly the 30-minute hold I originally
measured and reported to Jesse. The review's read of this is correct.

**New: the `consecutive_failures=2` anomaly, fully explained.** The review
flagged this as "likely also explains... although the exact increment
behavior should be confirmed in the DB method." Confirmed precisely: both
writers call the *same* `record_source_failure()` method, and its UPSERT
does `consecutive_failures = source_health.consecutive_failures + 1` on
every conflict, unconditionally. One challenged title therefore produces
**two** increments in the same request — Writer 1's cooldown write, then
Writer 2's blocked write — which is exactly why the live evidence row
showed `consecutive_failures=2` for what was actually a single challenge
event, not two.

### 7. RSS path does not call the second writer — confirmed

```
grep -rn "record_scrape_outcome(" backend/ --include=*.py
  backend/api/routes/downloads.py:262
  backend/api/routes/downloads.py:313
  backend/download_service.py:2259
```

`backend/hdencode_action_service.py` has zero references to
`record_scrape_outcome` or `source_health`. The manual-download route and
`download_item()` are the only callers. This confirms the review's
qualification: RSS-driven grabs and manual downloads share the coordinator
gate (so a challenge from either poisons both), but only the manual path
currently writes the second, overwriting health record. Resolving the
duplicate-writer defect (review's recommended option 1: coordinator owns
all HDEncode challenge/cooldown persistence, `record_scrape_outcome()`
skips persistence for coordinator-owned diagnostics) makes both paths
converge, as recommended.

### 8. Static `_MESSAGES`, whole-source blocking, RSS conclusion

All three already matched my proposal's own conclusions and are unchanged
by this verification pass — no new evidence contradicts them.

## Outstanding: none

Nothing in the review conflicts with the real checkout. No counter-findings
to raise. The design in `CLAUDE_REPLY.md` — the 10-step implementation
order, the 14 required tests, the typed `cooldown_until`/`cause_code`
fields, `SOURCE_TEMPORARILY_BLOCKED`, the single-authoritative-writer fix —
is confirmed sound and ready to implement as-is.

## Next step

Per the established lane split, this is ready for ChatGPT to produce the
actual implementation as a guarded code package (apply.py + expected
head/blobs), which I'll validate against a real checkout, apply, test, and
push to this review branch — no merge/deploy without Jesse's explicit sign-off.
