# HDEncode traffic-denial misreporting — findings and proposed fix

**Status:** proposal only, no code changed. Written up for ChatGPT design
review before implementation, per the established Claude=validate/deploy,
ChatGPT=design/write lane split. Triggered by a live user report ("my
downloads keep failing") during the RSS-shadow qualification window.

Evidence: [`hdencode-traffic-denial-evidence/source_health_hdencode_2026-07-22T2250Z.json`](hdencode-traffic-denial-evidence/source_health_hdencode_2026-07-22T2250Z.json)

## 1. What the user saw

A manual 14-item download batch (browse UI, not RSS-driven) failed: 13 items
showed **"HDEncode is disabled in Settings; no request was made"**, and one
showed **"The source presented an interactive verification challenge that
did not clear."** `hdencode_enabled` was confirmed `true` both on disk and
via the live `/settings` API at the time. A retry ~10 minutes later, on the
challenged title alone, failed again with the same "disabled in Settings"
message.

## 2. Root cause, fully traced

**Timeline (all app-log bracketed timestamps are America/New_York; UTC in
parens):**

| Time | Event |
|---|---|
| 18:38:11 (22:38:11 UTC) | Last successful HDEncode request |
| 18:38:21 (22:38:21 UTC) | *"I'm Gonna Git You Sucka"* hits an interactive verification challenge |
| 18:38:21 (22:38:21 UTC) | `source_health` row for `hdencode` written: `state=blocked, reason_code=interactive_challenge, consecutive_failures=2, cooldown_until=NULL` |
| 18:38:21–18:40:02 | The other 13 items in the same batch each fail "disabled in Settings" |
| 18:48:48 (22:48:48 UTC) | User retries the challenged title alone — same "disabled in Settings" message |
| (checked) 22:50:10 UTC | `hdencode_enabled` confirmed `true` on disk and via live API |

The `source_health.updated_at` timestamp (`22:38:21.359242+00:00`) is an
exact match for the challenge failure, confirming causation, not
coincidence.

**Code path** (`backend/hdencode_coordinator.py`):

`HDEncodeTrafficCoordinator._active_decision()` returns `blocked=True` for
**four distinct reasons**:

```python
def _active_decision(self) -> HDEncodeDecision:
    if not self._enabled():
        return HDEncodeDecision(True, "disabled", "source_disabled")
    ...
    if local_until and local_until > now:
        return HDEncodeDecision(True, "cooldown", "local_cooldown", local_until.isoformat())
    ...
    if state == "cooldown" and cooldown_until and cooldown_until > now:
        return HDEncodeDecision(True, state, reason, cooldown_until.isoformat())
    if state == "blocked":
        # Legacy blocked records did not always include an expiry. Hold them
        # for thirty minutes from the last update, then permit one probe.
        until = cooldown_until or (updated + timedelta(minutes=30) if updated else None)
        if until and until > now:
            return HDEncodeDecision(True, state, reason, until.isoformat())
```

In this incident, `cooldown_until` was `NULL` on the persisted health row, so
it fell into the **legacy `"blocked"` branch**: a 30-minute hold from
`updated_at` (expires ≈ 23:08:21 UTC), computed correctly and returned as
`HDEncodeDecision.until`.

Every one of the three real-denial call sites raises the same way:

```python
raise HDEncodeTrafficDenied(
    decision.reason_code or decision.state,
    f"HDEncode traffic is {decision.state}",
)
```

So `exc.code` **is** correctly `"interactive_challenge"` (from the persisted
`reason_code`) or `"source_disabled"` depending on cause — the coordinator
already knows and correctly reports the real reason. **The `until` timestamp
computed above is never attached to the exception at all** — it's
computed, returned in the `HDEncodeDecision`, and then dropped.

**Where it gets collapsed** (`backend/download_service.py:1276`):

```python
except HDEncodeTrafficDenied as exc:
    return None, ScrapeDiagnostic(
        ScrapeCode.SOURCE_DISABLED,      # always this code, regardless of exc.code
        retryable=False,                  # always non-retryable, even for a 30-min hold
        affects_source_health=False,
        signals=(exc.code,),              # the real reason survives only here, unused downstream
    )
```

`exc.code` is captured into `signals` but never inspected. Every
`HDEncodeTrafficDenied` — config-off, in-process cooldown, DB-persisted
cooldown, or this 30-minute legacy hold — is reported identically as
**"HDEncode is disabled in Settings"**, permanently non-retryable. That
message is only true for one of the four cases.

**Why 13 unrelated titles failed too:** `_active_decision()` gates the whole
`hdencode` source, not a specific URL — this is almost certainly intentional
(continuing to hit a site that just challenged you, with many parallel
requests, is the wrong response to a bot-detection signal). Flagged in §5 as
something to confirm rather than something this proposal changes.

## 3. Proposed fix

**Key discovery that shapes this proposal:** `backend/scrape_outcome.py`
already defines `ScrapeCode.INTERACTIVE_CHALLENGE = "interactive_challenge"`
with the message *"The source presented an interactive verification
challenge that did not clear."* — and the coordinator's `reason_code` for
this exact case is the literal string `"interactive_challenge"`. Same for
`ScrapeCode.SOURCE_DISABLED.value == "source_disabled"` matching the
coordinator's disabled-case `reason_code`. **These are not coincidental
near-matches — `exc.code` already speaks the `ScrapeCode` vocabulary.** The
bug is that `download_service.py` never looks it up; it hardcodes
`SOURCE_DISABLED` regardless. So the minimal, most defensible fix reuses
existing, already-correctly-worded codes rather than inventing new ones,
falling back to a single new generic code only for reasons that have no
existing match (e.g. a bare rate-limit cooldown with no more specific
`reason_code`).

**3a. Carry `until` on the exception** (unchanged from the first draft —
this part is still needed since `decision.until` is computed but currently
dropped entirely):

```python
# hdencode_coordinator.py -- both call sites already have `decision` in scope
raise HDEncodeTrafficDenied(
    decision.reason_code or decision.state,
    f"HDEncode traffic is {decision.state}",
    until=decision.until,   # new
)
```
(`HDEncodeTrafficDenied.__init__` gains an optional `until: Optional[str]`
kwarg, stored as `self.until`.)

**3b. Look the code up instead of hardcoding it.**

```python
# download_service.py
except HDEncodeTrafficDenied as exc:
    try:
        code = ScrapeCode(exc.code)          # reuses INTERACTIVE_CHALLENGE,
        retryable = code is not ScrapeCode.SOURCE_DISABLED   # SOURCE_DISABLED, etc. as-is
    except ValueError:
        code, retryable = ScrapeCode.SOURCE_TEMPORARILY_BLOCKED, True  # one new fallback code
    return None, ScrapeDiagnostic(
        code, retryable=retryable, affects_source_health=False,
        signals=(exc.code, exc.until),
    )
```

**3c. One new fallback code**, only for `reason_code`/`state` values that
don't already have a `ScrapeCode` match (e.g. a generic
`"local_cooldown"`/`"cooldown"` triggered purely by 403/429/503 volume, with
no more specific named cause).

**Open question, checked and confirmed, not resolved by this proposal:**
`ScrapeDiagnostic.public_message` is currently a plain dict lookup —
`_MESSAGES[self.code]`, a `Dict[ScrapeCode, str]` — it does **not** support
callables today. So the `until` timestamp (from 3a) cannot be embedded into
`public_message` without either (a) changing `_MESSAGES`'s type to allow a
`str | Callable[[ScrapeDiagnostic], str]` value, or (b) leaving
`_MESSAGES[SOURCE_TEMPORARILY_BLOCKED]` as a static, generic string ("...will
retry automatically.") and letting the `until` value travel only through
`signals` (already present in `to_dict()`'s `"signals"` list) for the
frontend to format if it wants to show a specific time. Option (b) is
smaller and lower-risk; (a) is more capable but touches a class used
elsewhere. No opinion asserted here — this is exactly the kind of call this
review is being requested for.

**Net effect:** the challenged title, and everything collaterally blocked
alongside it, would report *"The source presented an interactive
verification challenge that did not clear"* (the existing, already-accurate
message — for free, since `ScrapeCode.INTERACTIVE_CHALLENGE` already carries
it) rather than the misleading "disabled in Settings," and would be marked
retryable. The config-disabled case is untouched (still correctly
permanent/non-retryable until a human re-enables it) since `SOURCE_DISABLED`
is excluded from the retryable set by construction, not by a separate
special case.

## 4. Testing plan (if this design is accepted)

Unit-level: extend `hdencode_coordinator.py`'s existing test suite with a
case per `_active_decision()` branch confirming `exc.code` and `exc.until`
propagate correctly (disabled / local cooldown / persisted cooldown / legacy
30-min block). Extend `download_service.py`'s scrape-outcome tests to assert
the new code/retryable split. No production or live-traffic testing
required — this is pure result-classification logic, fully unit-testable
against the coordinator directly (matches the real-process-testing standard
from the PR #21 review — no browser/network access needed here since the
denial happens *before* any transport attempt).

## 5. Explicitly NOT proposed — flagging for confirmation, not changing

The whole-source (not per-item) blocking behavior on a single challenge is
left as-is. Removing or narrowing it could mean continuing to send several
more requests to a site that just told the app to back off — plausibly worse
for the account/IP than the current behavior. If this reading of the intent
is wrong, that's a different, separate design conversation from the
messaging fix above.

## 6. "Wouldn't RSS discovery have helped with this?" — no, and here's why

Verified in code, not assumed: the RSS-candidate grab path
(`hdencode_action_service.py::run_action`, used once RSS auto-grab is
enabled) calls the *exact same* method as a manual browse download —

```python
with self.coordinator.prioritize(...):
    scraped = self.download.scrape_links(action["canonical_url"], ...)
```

— through the *same* `DownloadService.scrape_links()` and the *same*
process-wide `HDEncodeTrafficCoordinator` singleton, and explicitly catches
the same `HDEncodeTrafficDenied`/`HDEncodeRequestCancelled` exceptions.

RSS mode's actual job is replacing the **discovery** step — finding which
releases exist — with a feed poll instead of scraping listing pages. It does
**not** touch the **link-retrieval** step: turning a known release into an
actual downloadable host link (Rapidgator, etc.), which always requires
visiting that item's own page with a browser, regardless of how the release
was discovered. That visit is exactly where the interactive challenge fired.
Since both entry points funnel through the same coordinator and the same
per-source health record, a challenge triggered by an RSS-driven grab would
equally poison manual downloads for the same 30-minute window, and vice
versa — they are not independent, isolated channels; they share the same
gate.

So: RSS discovery mode reduces *listing-scrape* traffic (its actual, proven
purpose this whole qualification effort has been validating) but would not
have prevented this specific class of failure, which happens one step later
in the pipeline, on a step every download — RSS-sourced or not — must pass
through.

## 7. Request

Design review requested before any implementation: is the 3a/3b/3c approach
sound, is `ScrapeCode.SOURCE_TEMPORARILY_BLOCKED` (or a different existing
code) the right vehicle, and does the §5 whole-source-block assumption match
intent? No code has been changed; this is a proposal only.
