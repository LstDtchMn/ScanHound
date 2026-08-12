# Review request — two queue findings from a live production investigation

**Repository:** `LstDtchMn/ScanHound`
**Base:** `6a4eb00` (`main`)
**Date:** 2026-08-12
**Origin:** not a code sweep — these came from investigating why real downloads were parked in the
live queue after the Turnstile verification hold fired for the first time in production.

## Context: what happened in production first

The verification hold worked exactly as designed and is **not** the subject of this review — it is
the corroborating evidence that the rest of the recovery model is sound where it applies:

- 2026-08-11 15:38Z, `Winnie the Pooh: Blood and Honey` (hdencode) hit a genuine Cloudflare
  challenge. Detection fired on the designed conjunction (`access_control_present`,
  `turnstile:response-field`, `turnstile:console-600`, `reveal-tier:not-ready`) and armed
  `verification_hold_source='hdencode'` on batch `9652110c`.
- The batch cooldown expired at 16:38Z and the item **did not auto-resume for 25+ hours** — the
  point of the feature.
- A single operator probe via `retry_item` succeeded, `source_reveal_succeeded` fired,
  **the hold released itself**, and the item completed ("Sent 1 links to JDownloader").

While clearing the remaining queue, two separate issues surfaced. Queue is now 312/321 completed.

---

## Finding 1 — MEDIUM: a batch with auto-resume disabled parks forever AND is excluded from the
## diagnostic built to prevent exactly that

**Location:** `backend/download_queue.py` — `_warn_exhausted_batches()` selection query.

`auto_resume_after_cooldown` is a **per-batch** value chosen at creation
(`backend/api/routes/downloads.py:210-221`), defaulting to the config
`download_queue_auto_resume_after_cooldown` whose default is **False**
(`backend/config.py:480`). In the UI it is a checkbox labelled **"Resume once after cooldown"**,
default **unchecked**, persisted in **localStorage** (`frontend/src/routes/downloads/+page.svelte:190,
376, 435-441`) — so it is per-browser/per-device, not per-account.

Observed live: batches created minutes apart carry different values
(`18:20 → 1`, `18:22 → 1`, `18:23 → 0`), confirming it is a per-request UI choice rather than a
config change over time.

**The defect.** `_warn_exhausted_batches()` selects:

```sql
WHERE state = 'paused_source'
  AND auto_resume_after_cooldown = 1
```

Its own docstring says it exists because "a due batch could stay parked forever without saying why"
and that "the tests said the batch should 'stop and wait for a human'; nothing told the human."
But it only covers batches whose auto-resume budget was **spent**. A batch with
`auto_resume_after_cooldown = 0` can *never* resume automatically at all — a strictly worse parked
state — and is filtered out of the warning entirely.

**Live evidence.** Batch `eaeb62a5` (`The Chosen in the Wild with Bear Grylls`), one item,
`state='paused_source'`, `auto_resume_after_cooldown=0`, `auto_resume_used=0` (budget untouched, so
this is NOT budget exhaustion), `cooldown_until=2026-08-10T19:14Z`. It sat with an **expired
cooldown and no recovery path for ~2 days**, invisible to the warning, until a manual `retry_item`
completed it immediately (3 links delivered) once the source was healthy.

**Questions for the reviewer:**
1. Should the warning cover `auto_resume_after_cooldown = 0` batches whose cooldown has expired?
   Our reading: yes — it is a *more* permanent parked state than the one already warned about. Is
   there a reason it was deliberately scoped out that we are missing?
2. Is a **localStorage-backed, default-off** per-batch checkbox the right home for a setting whose
   failure mode is "this batch silently never recovers"? It is per-device, so the same user gets
   different durability from their phone vs their desktop, with no indication.
3. The label "Resume once after cooldown" describes the enabled behaviour. Should the *disabled*
   behaviour be surfaced (e.g. the batch is marked "manual recovery only"), given the consequence is
   permanent invisible parking rather than merely "no auto-retry"?
4. Is there a defensible invariant here, e.g. **"any `paused_source` batch whose cooldown expired
   more than N minutes ago must be reported, regardless of its auto-resume flag"**?

---

## Finding 2 — LOW (diagnostics/naming): `layout_changed` is a catch-all that misdirects triage

**Location:** the reveal-failure classifier feeding `last_reason_code` / `last_message`.

Live item `The Young Riders` (hdencode) is terminal `failed` with:

```
last_reason_code      layout_changed
automated_retry_count 12
last_message          "The link-reveal control was not on the page, and this was not a recognised
                       verification challenge. The page may be a login or region gate, an
                       unrecognised block, an error page, or a changed layout."
signals               access_control_present        (NOTE: no turnstile:* signals)
```

The *message* is careful and honest — it lists four possibilities. The *reason code* commits to one
of them (`layout_changed`), and that is the field that shows up in triage.

**Why we think it is not a layout change:** the same source has **312 completions**, three of them
today, and only this one row plus 7 cancelled rows carry `layout_changed`. A genuine layout change
would not be release-specific. The signal set (`access_control_present` with no Turnstile markers)
points to a per-release access gate — login, region, or a pulled release.

**Questions for the reviewer:**
5. Should the unrecognised case get its own reason code (e.g. `reveal_control_absent_unclassified`)
   rather than being folded into `layout_changed`? Our concern is the inverse error: a *real*
   site-wide layout change and a one-off gated page currently produce the same code, so neither can
   be detected by aggregating it.
6. Is there a cheap discriminator worth recording — e.g. "did OTHER items from this source succeed
   within the same window?" — that would let the code distinguish "this page" from "this site"?
   (In this incident that question was decisive, and we had to answer it by hand.)

---

## What we are NOT claiming

- No claim that the verification hold, the recovery policy precedence, or `retry_item` are wrong —
  all three behaved correctly under live conditions here.
- Finding 2 is a diagnostics/naming concern, not a correctness bug; the classifier's *message* is
  accurate and it is the *code* and its aggregability we are questioning.
- We did not fetch the affected release page (it would be a live request to the source), so the
  per-release-gate conclusion is inference from signals plus the 312-success base rate, not a
  direct observation.
