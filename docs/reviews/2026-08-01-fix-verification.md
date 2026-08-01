# Fix verification — the four checks requested before Part 9

**Branch head:** `b57484b` · **Fixes commit:** `71c54ce` · previous review head `f1d9997`

You asked for four things before authorising Part 9. Three are answered here
with executed evidence; the fourth is a commitment.

---

## 1. Verification of the actual fixes, not the response document

Diffs `f1d9997 → HEAD`, comments stripped.

**Blocker 1 — coverage must require an actual normal-feed observation**
```diff
-    elif rss_state in (RssAcquisition.GREEN, RssAcquisition.YELLOW):
+    elif first_normal is not None:
```

**Blocker 2 — completion must prove the whole uncertainty interval crossed**
```diff
-        oldest = min(timed, key=lambda t: t.earliest_possible)
-        if oldest.earliest_possible <= stop_target:
+        oldest = min(timed, key=lambda t: t.newest_possible)
+        if oldest.newest_possible <= stop_target:
```
with `newest_possible` added as the derived absolute, since listing times round
DOWN — `"2 days ago"` means a true time in `(NOW−3d, NOW−2d]`.

**Blocker 3 — reconciliation unconditional.** Extracted into a pure, importable
`reconciliation_blockers()` so it can be tested directly rather than inferred
from a code read — a gate whose only proof is a code read is exactly how this
shipped failing open twice:
```python
if missing_credentials:
    return [f"NO AUTH TOKEN at {token_name} — the independent readiness "
            "cross-check could not run; qualification requires it"]
```

**Blocker 5 — the invented constant cannot influence qualification**
```diff
+VOLUME_ANOMALY_ENABLED = False
+    volume_anomaly_enabled: bool = VOLUME_ANOMALY_ENABLED,
-    if expected_typical and posts_found < expected_typical * volume_fraction:
+    if (volume_anomaly_enabled and expected_typical
+            and posts_found < expected_typical * volume_fraction):
```

---

## 2. Regression tests fail on the old implementation, pass on the corrected one

This is the check that matters most, so it is executed rather than asserted.
`tests/tools/mutation_check.py` restores each defective implementation **in
place**, runs only the tests meant to catch it, requires them to FAIL, then
restores the fix and requires them to PASS.

```
[DISCRIMINATES] blocker 1 — coverage keyed off colour instead of first_normal_at
          corrected -> PASS   (2 passed in 0.15s)
          defective -> FAIL   (2 failed in 0.11s)
[DISCRIMINATES] blocker 2 — completion used the oldest possible edge
          corrected -> PASS   (1 passed in 0.10s)
          defective -> FAIL   (1 failed in 0.10s)
[DISCRIMINATES] blocker 3 — reconciliation conditional on a token existing
          corrected -> PASS   (2 passed in 0.10s)
          defective -> FAIL   (2 failed in 0.09s)
[DISCRIMINATES] blocker 5 — volume anomaly on by default with an invented constant
          corrected -> PASS   (1 passed in 0.13s)
          defective -> FAIL   (1 failed in 0.14s)

RESULT: all 4 regression tests fail on the old implementation and pass on the
corrected one
```

The harness is committed, so this is repeatable rather than a claim about a
session that has ended.

**Worth stating plainly: my original tests would have failed this check.** The
old coarse-timestamp test used *hour* granularity, where a 48 h reading clears a
47 h target on **both** edges — it passed under the correct rule and the broken
one. It had zero discriminatory power and I did not notice, because I read it as
"there is a test for coarse granularity" rather than asking what it could
distinguish.

Your phrasing is the better one and I have adopted it:

> A test that cannot distinguish the intended behaviour from a known incorrect
> implementation creates false confidence. It is more dangerous than an absent
> test because it appears to validate the code while providing no discriminatory
> power.

---

## 3. Final full-suite results after the environment fixes

```
4392 passed, 4 skipped, 1 warning in 586.47s   (exit 0)
FAILED: NONE
```

**A correction, because I got this wrong twice before arriving here.**

My earlier runs reported 47, then 14, then 2 failures. I attributed the final
two to "tests asserting behaviour when `plyer`/`selenium` are absent, and both
are present in the image". **That was wrong, and I inferred it from the test
NAMES rather than reading the failure output.** The actual message was:

```
async def functions are not natively supported.
```

The real cause: **I had never copied `pytest.ini` into the test container.** The
project sets `asyncio_mode = auto`; without that file pytest ran in
`Mode.STRICT`, so async tests lacking an explicit marker were collected and then
failed. Every suite figure I quoted today — including the 4361 and 4390 — was
produced under a harness missing the project's own pytest configuration.

With `pytest.ini` present, all of it passes: **4392 passed, 0 failed.**

I am flagging this rather than quietly reporting the clean number, because it is
the same error as the two defects you caught, one level up: **I explained a
result instead of reading it.** It happened while I was writing a document about
that exact discipline. The corrected figure above is from a container populated
with `backend/`, `tests/`, `scripts/`, `docs/`, `frontend/src` **and**
`pytest.ini`.

The mutation check in §2 was re-run under the corrected configuration and still
reports all four fixes discriminating.

---

## 4. The qualification window begins only from this corrected implementation

Agreed, and it is not merely policy — the previous window's evidence is
unusable regardless, because the readiness cross-check that was supposed to
corroborate it had never once succeeded.

No reuse, no continuation. A complete fresh gate: seven calendar days, 20 valid
cycles, zero relevant misses, positive measured request reduction, healthy
feeds, restart and catch-up evidence. Any absent, failed or disagreeing
application-readiness cross-check is a mandatory stop — now enforced in code and
tested, including the missing-credentials case that previously slipped through.

---

## Residuals I am still declaring

* **`expected_typical` has no producer.** Volume-anomaly detection does not
  operate, and is now off behind a flag so it cannot be mistaken for protection.
  Wiring plus calibration from recorded live listing volumes is a Part 9
  prerequisite, per your ruling.
* **`disjoint_identity_sets` catches only total non-overlap.** A partial
  canonicalisation failure still surfaces as ordinary `listing_only` misses.
  Acceptable only because a relevant miss is already a mandatory stop.
* **Still no CI on the branch.** These numbers are produced by me in a
  throwaway container. The mutation harness is committed so you can re-run it,
  but nothing here is machine-attested. Given §3, that limitation is not
  theoretical: an incompletely configured local harness produced three
  successive wrong failure counts before I noticed. CI would have caught the
  missing `pytest.ini` on the first run. I would treat "add CI to this branch"
  as a genuine recommendation rather than a formality.
* **One pre-existing flaky test**, unrelated to this branch and logged
  separately: `test_new_lifespan_cancels_waiting_rss_before_transport_construction`.

Nothing is deployed. Part 9 remains Jesse's gate and I am not asking for it.
