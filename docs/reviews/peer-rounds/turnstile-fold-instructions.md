# Turnstile fold — prepared instructions for a dedicated pass

**Date:** 2026-08-10
**Prepared by:** Claude (session `e7d059a1`), from the handoffs of `54093368` and `b087aa20`
**Branch:** `agent/turnstile-consolidation`, created off the agreed base, **nothing folded yet**

**Everything here is the two branch authors' own conclusions, reached independently and agreed
between them.** I have not reviewed either branch; my contribution is the conflict survey and
this document. Where they disagreed with each other, they settled it on the merits — including
`b087aa20` arguing against its own branch.

---

## The decision

**Base:** `claude/scanhound-turnstile-verification-hold-z43q0x` @ `c05186c` (8 commits from
`main`, first is `a88d541`).
**Fold in:** `agent/turnstile-classification` @ **`6b0b1a5`** (was `2bac41c` when first handed off; two documentation commits landed after. Conflict count is unchanged at 17, so the fold shape is identical).

**Why, in their words** — `b087aa20` found the deciding fact against itself: its hold closure
fires only from `_complete`, never `_fail` (verified, zero references in the `_fail` body), so a
*successful* reveal followed by a failed JDownloader handoff leaves the hold open and every
sibling parked on stale evidence. The base's `source_reveal_succeeded` fires from both paths.

`b087aa20` also withdrew an earlier claim to me that its branch was an "architectural superset"
with the other having "zero episode semantics" — it had grepped for its own naming
(`challenge_episode`) instead of reading the other's code. **Disregard that comparison.**

## Conflict survey (17 hunks, 10 files) — measured, then aborted

| File | Hunks | Character |
|---|---:|---|
| `backend/queue_recovery_policy.py` | 4 | **prose only** — same logic, different comments |
| `frontend/…/VerificationRetries.svelte` | 3 | to inspect |
| `backend/download_queue.py` | 2 | **REAL divergence — see below** |
| `scripts/queue_recovery_state.py` | 2 | to inspect |
| `backend/browser_adapter.py` | 1 | docstring only; **keep base's** — it names the 600\*-family console signal, which is the detection that actually fires |
| `backend/download_outcome.py` | 1 | import superset — take `Callable` |
| `backend/database.py`, `download_service.py`, `scrape_outcome.py`, `tests/test_download_queue_and_browser.py` | 1 each | to inspect |

The designs converged hard, so most divergence auto-merged and most conflicts are wording. Do not
mistake the file count for difficulty — but do not take either side wholesale either (see the
regression risk below).

### The one real divergence

`backend/download_queue.py`: the base **excludes held rows and reports the count** for the UI;
the other **raises `DownloadQueueError`** with an operator message.

**Both authors agree this is a reconciliation, not a winner:** keep `retry_ready`'s
exclude-and-report UX **and** add the `resume_batch` raise. The base's `resume_batch`
non-automated branch promotes all deferred rows **without calling `decide()`** — an unguarded
second door that fans a held batch into the challenge.

## REQUIRED READING BEFORE STARTING

`SCANHOUND-TURNSTILE-SESSION-HANDOFF.md` on `agent/turnstile-classification` @ `6b0b1a5`:

- **§8a — the peer-coordination outcome.** The joint decision and why, the `_fail` closure defect
  that decided it, the withdrawn "architectural superset" claim, the port list with each gap
  confirmed by the other session, and the migration split. Written specifically so a reader
  arriving at that document does not use its *earlier* comparison to conclude the opposite.
- **§8b — every dead end, consolidated.** Detection approaches that failed (Cloudflare's
  documented markup is absent from this site; the challenge iframe is a race, not a signal; no
  shadow roots; the dormant `api.js` ships on healthy pages), refuted hypotheses (`postMessage`,
  a benign integration defect, and "no captcha or Cloudflare is involved" — false, and
  expensively so), methods that failed (reloading, six times; 10-minute polling is what worked),
  and its author's own six mistakes with what caught each.

**If the fold is about to re-run any diagnostic ground, read §8b first.** It exists because that
session's full dead-end list was otherwise only in a chat that has now been archived.

## Port these four from `agent/turnstile-classification`

1. **`clear_challenge_episode()`** — an explicit operator abandon, ~15 lines. The base has **no
   equivalent**: its only clear is a reveal success, so a permanently-challenged source
   deadlocks, and the only exit is cancel.
2. **The `resume_batch` guard** — as above.
3. **The stricter test harness** — its 22-item negative control **raises** on any unscripted
   `download_item` (the base asserts `call_count == 1`, which is softer), plus the paired
   positive control proving the rig can promote when nothing is held. The base lacks that control.
4. **Diff `_form_posts_unlock`** against the base's reveal-form logic.

Also **keep both migrations**: the base's inline v9 (correct going forward) and
`scripts/migrate_challenge_episode.py` with explicitly named IDs (the auditable path for a named
incident).

## The highest-risk item — a required test

`b087aa20`'s first interstitial/embedded partition **over-narrowed and demoted genuine Cloudflare
interstitials** from `INTERACTIVE_CHALLENGE` to `LAYOUT_CHANGED` — "a challenge blocked us" became
"the scraper is broken." **Both branches' partitions have that shape.** Resolving it by taking
either side wholesale brings the regression back silently.

**Required case: body-only interstitial, no `<title>`, iframe present → still
`INTERACTIVE_CHALLENGE`.** It was caught by the full suite, not by review, because "just a moment"
is matched against `<title>` and a body-only interstitial has only iframe evidence. The base's
`test_scrape_outcomes` covers title-based interstitials but **not** this one.

## Dead ends — do not re-derive

- **Reloading to catch the active stall FAILS.** Both sessions hit this: a single probe, and 3–6
  consecutive reloads, find the reveal healthy because the challenge is **intermittent**. Only a
  time-spaced poller caught it — `scripts/turnstile_watch.py`, 10-minute polling, read-only, on
  cycle 20 (~3+ hours).
- **Cloudflare's documented markup finds nothing here.** `.cf-turnstile`, `data-sitekey` and a
  queryable iframe are all absent — hdencode renders Turnstile programmatically in invisible
  mode. Only the unsolved `cf-turnstile-response` field and the `600*` console line fire
  reliably. Found independently by both.
- **Migration placement:** the base's first v9 ran *before* `download_queue_items` existed on a
  fresh DB → "no such table" on every fresh init. A live v8 DB would have hidden it. Fixed, but
  it is the class of bug that only shows on a clean database.
- The invisible-widget teardown/rebuild cycle (~11 s) explains why the container and iframe legs
  usually do not fire.

## Unverified / open — carry into the combined round

- The base's **v9 migration is a no-op on current live data**: the parked rows are
  `reveal_verification_stalled`, predating the classifier, so no
  `verification_required` + `interactive_challenge` trigger row exists. Correct behaviour, but the
  migration path is **unexercised against real rows**.
- **Cross-batch containment beyond auto-resume leans on the coordinator's in-memory cooldown**,
  which resets on container restart. True of **both** branches.
- **A non-Turnstile captcha frame on a not-ready reveal classifies source-wide** in both branches
  — a generic frame carries no form association to test.

## Evidence that exists (do not redo)

The base carries the **only live active-stall capture** across both branches: console `600010`
plus all `challenges.cloudflare.com` requests returning HTTP 200 → automation rejection, which
**ruled out an integration defect**. See
`docs/reviews/peer-rounds/turnstile-active-stall-capture-2026-08-10.{json,md}`, plus
`2026-08-09-verification-hold-implementation.md` and its round-2 response.

## Date bomb

`a88d541` is the keeper and is already commit 1 of the base **and** already in the DV
consolidation. `b087aa20`'s `fix/policy-tests-wall-clock` (`9f28ba4`) is **confirmed redundant by
both authors** — same autouse mechanism, and `a88d541` adds a load-bearing far-side-of-FUTURE
test plus a measured canary. `b087aa20` asked that Jesse or the consolidating session close the
branch rather than deleting it itself. **Not closed yet — Jesse's call.**

## Reviewer available

`b087aa20` offered to **review the folded combination as a peer rather than as its author**,
which is more useful than having folded it itself. Take that offer.

## How to start

```
git worktree add <path> agent/turnstile-consolidation
cd <path>
git merge --no-commit --no-ff origin/agent/turnstile-classification
```

Use a **worktree**, not the main working tree: these branches are based on `main`, which does not
contain `scripts/run-dv-scan.ps1`, and checking one out in the main tree would delete the wrapper
the `ScanHound-DVScan` scheduled task executes.
