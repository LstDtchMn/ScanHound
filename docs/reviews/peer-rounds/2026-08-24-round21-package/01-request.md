# Round 21 — review request

You are an equal peer, not a rubber stamp. Disagreement is expected. Where you
think I am wrong, say so directly and show the reasoning; where you think a
design is right but the implementation does not achieve it, that is the most
valuable finding of all — the last five rounds have turned up exactly that.

Please preserve finding identity across rounds (R21-1, R21-2, …) and state
OPEN / CLOSED / PARTIALLY CLOSED for anything you carry forward.

---

## 1. The claims I most want challenged

### C1 — Quarantine does not remove the row, and I think the obvious design is wrong

`listing_claims_quarantine` records a snapshot and a reason, but the row **stays
in `listing_claims`**, still carrying its legacy key and still having empty
version columns.

My reasoning: unattributable evidence may *narrow* authority but never *widen*
it. A row whose arm cannot be determined can still contradict a claim — a
movie/TV disagreement is visible without knowing which feed reported it.
Deleting the row removes that contradiction and makes a negative claim easier to
sustain, which is widening authority by omission.

The counter-argument I can see, and want you to press: the row now carries an
`arm_id` that names a feed which does not exist (e.g. `ddlbase:remux`), so any
consumer grouping by `arm_id` will display a phantom arm. Is "honest but
phantom" better or worse than "removed but auditable"? Is there a third option I
have missed — for example a nullable `attributable` column — and does that third
option actually change any behaviour, or merely rename the problem?

### C2 — Two versions, and whether the second is really load-bearing

`request_definition_version` is a digest of the canonical request.
`parser_version` is supplied by the *caller* at write time, not read from the
spec, on the reasoning that the running parser's version is a fact about the
process doing the reading. A spec declaring `select_posts/1` while the process
runs v2 therefore produces a v2 revision and records the mismatch.

Attack: does that actually record anything useful, or does it just guarantee
that a parser upgrade silently orphans every existing row into a
non-proof-eligible state with no operator-visible signal? What *should* happen
to a ledger on the day the parser version changes? I do not have an answer I am
confident in, and I would rather have it challenged now than discover it after a
parser change.

### C3 — The split between shape and attribution

Shape migration runs automatically inside `_init_db` and rebuilds a deployed
table (create → copy → count-check → drop → rename). Attribution is separate,
gated, and dry-run by default.

Attack the automatic half specifically. It rebuilds a live table on every
container start where the old shape is found. I check `COUNT(*)` before and
after and refuse on mismatch, but:
- Is a count check sufficient, or should it compare content?
- The old primary key is a strict prefix of the new one, so I argue the rebuild
  *cannot* legitimately lose a row. Is that argument sound?
- Is doing this in `_init_db` at all defensible, versus refusing to start and
  requiring an operator command?

### C4 — Pagination as declared data

`_crawl_pages` has four branches, not two, and the `adithd` branch **drops the
query suffix** on page N. I encoded the branches as a `PaginationForm` enum
inside the hashed request definition, and a test rebuilds every page URL from
the declared form and compares it against a copy of the crawler's own
f-strings.

Attack: that test copies the branches rather than importing them, because they
are inline in a long method. That copy can drift. Is the drift risk worse than
the tautology I would create by importing the very code under test? Is there a
third framing — extracting the branches into a function both use — and does that
make the test vacuous?

### C5 — Atomicity

Plan and apply share one transaction, and I verified rollback by *behaviour*:
an injected failure two-thirds of the way through discarded all 161 rows already
written (see `03-evidence.md` §5). I did this because sqlite3 in autocommit mode
would make `rollback()` a silent no-op while the docstring still claimed
atomicity.

Attack: is one transaction spanning a whole migration the right granularity for
a table that could be much larger than 266 rows? At what size does this become a
lock-duration problem, and what is the honest answer for a ledger that grows?

---

## 2. Specific places I expect bugs

Past rounds found defects in code *adjacent* to the change, not in the change
itself. The adjacent code here is:

1. **`backfill_listing_claim_posted_dates`** — I widened its join from
   `arm_key` to all three revision columns. If the claim and alias rows ever
   disagree on a version column, the join now silently matches nothing and the
   backfill becomes a no-op that reports success. Is that reachable?
2. **`record_listing_claims` fallback** — a claim with no stamped arm still
   writes, using the legacy two-part string as its `arm_id`. That mints a
   colon-bearing id into a ledger whose ids are supposed to be dot-separated and
   opaque. I chose "record it, unattributed" over "drop it". Is the fallback
   reachable in production, and is the choice right?
3. **`resolve_descriptor`** does a linear scan recomputing SHA-256 over all nine
   specs per call, per source, per crawl. Correctness first, but is there a
   sharper objection than performance here?
4. **`arm.unregistered.<16 hex>`** truncates the digest to 16 hex characters.
   Argue about collision risk and about whether truncation is defensible at all
   in an identity.

---

## 3. Test adequacy

70 new tests in `tests/test_round20_arm_identity.py`. Two things I want judged:

- **The anti-vacuity controls.** Several tests carry an explicit companion
  asserting that the check *can* fail. I added these because my first draft of
  the `posted_date_raw` merge test could not distinguish the fix from the bug —
  both rows had the same `last_seen_at`, so the target won on a tiebreak rather
  than on recency. Are the controls I wrote actually sufficient, or are they
  decorative?
- **The two retired round-19 files** (606 lines). I claim every surviving intent
  is carried forward. Please check that claim against the patch rather than
  taking it; deleting tests is the easiest place to lose coverage silently.

---

## 4. What I am not asking

Do not review deployment readiness, and do not propose that anything be merged,
deployed, or enabled. Those decisions are Jesse's alone and are out of scope for
this round.
