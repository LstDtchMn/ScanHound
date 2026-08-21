# Round 15 request — M14-1 and M14-2 closed, ledger reshaped

## Verdict accepted

Round 14's ruling accepted in full. Nothing contested.

## M14-1 — you caught my tests being circular, and they were

This is the part worth reading first, because the mistake is more instructive
than the fix.

My round-13 restart tests seeded

```text
cache.category_conflict = true
downloads.media_kind    = movie
```

by hand and proved startup recovery handles that state. As you put it, they
proved *"if the journal exists, recovery works"* — never that the journal exists
after the failure it describes. Recovery wrote its conflict mark to the same
SQLite database whose erase had just been refused, so in exactly the case it
mattered nothing was written, startup saw no signature, and the stale authority
came back.

**My own code comment said a marker in the same file cannot protect that case.
The implementation depended on it anyway.** I wrote the caveat and then built
against it, which is a worse failure than not seeing it.

### Option A, with B as the fallback

**A — independent journal.** An append + `fsync` to a file beside the database,
written BEFORE the erase is attempted and confirmed after it lands. Its failure
semantics are independent of the transaction, which is the property you asked
for. A torn final line — what a crash mid-append actually looks like — is
skipped, and the rest of the journal survives.

**B — interlock, for the irreducible case.** Nothing is durable if the disk
refuses every write. If the journal itself cannot be written, this process can no
longer promise a restart will finish the job, so it stops promising anything:
`_authority_disabled` withholds EVERY identity, not just the affected release,
because we do not know what else was in flight.

I took A as primary rather than B alone because B degrades the feature after
every restart on a rule I could not specify precisely — "re-established by a
fresh trustworthy reconciliation" is exactly the kind of phrase I would have
implemented wrongly.

### The tests never seed the journal

They break the database, let production fail however it fails, then construct a
**new `DatabaseManager` on the same path** — a real restart with empty in-memory
holds — and ask whether authority is withdrawn.

```text
mutation: recovery ignores the independent journal
  -> fails with exactly the round-13 hole restated:
     "startup found no record of the interrupted revocation, so the stale
      authority is about to be served again"

mutation: journal failure no longer disables authority        kills 1
```

There is a positive control for the opposite direction too: a COMPLETED
revocation must leave nothing to recover, or every restart would wipe authority
from the whole library and the test above would still pass.

## M14-2 — cross-crawl contradictions revoke

Your framing is the part I had missed:

```text
positive evidence may NARROW authority immediately
authority may WIDEN only through a coverage proof
```

The crawl only ever saw disagreement WITHIN one `_crawl_pages()` call, because
`url_type_claim` lived and died there. Two sightings a week apart that disagree
are contradictory positive evidence just the same.

`consume_cross_crawl_conflicts()` finds opposite-type durable claims and routes
them through the existing `HOLD -> ERASE -> MARK` path rather than inventing a
second revocation route. `record_listing_claims()` stays an inert writer, and a
test asserts it still revokes nothing by itself.

The required case is tested exactly as you specified — run A records
`U -> movie`, run B records `U -> tv`, and the assertion is on
`annotate_source_links()`, not on the claims table.

## Ledger shape — all four changes, while the table has zero rows

```text
A  canonical_url identity via url_identity.canonicalize_listing_url
B  arm_key ("hdencode:tv") as stable raw arm identity; listing_type kept
   alongside as the code-derived snapshot it is
C  order_key renamed posted_date_raw
D  a DIFFERING later date sets posted_date_changed instead of being coalesced
E  first_seen_at / last_seen_at / sightings kept
F  coverage proof kept out of this table entirely
```

On **A**, the reason is sharper than tidiness: the ledger exists to detect that
one release was claimed by two arms, and filing two claims under different keys
would hide precisely the contradiction we are collecting them to find. There is a
test where the two claims arrive under different raw hrefs and the conflict is
still caught.

`raw_url` is kept for audit **and** for the join — `background_scan_cache` keys
on the raw href, so a canonical-to-raw join would silently match nothing.

On **C**, you were right that naming it `order_key` pre-blessed a decision the
coverage model has not made. It is the site's verbatim string, stored
unnormalised, named for what it is.

On **D**, the surviving mutant last round is what revealed this as a semantic
choice rather than an implementation detail — and the coverage model is about to
depend on that immutability, so silently choosing one value would bury the
evidence that it does not hold.

## A bug I made while reshaping

The replacement span left a **duplicate `listing_claim_summary`**, and the stale
later definition shadowed the new one. It surfaced as `no such column: source` in
a test rather than as anything obviously structural. Removed.

## The coverage model — still not built, and I accept why

`min(observed posted_date)` is rejected and the sticky-post counterexample is
decisive: a single old pinned entry on page 1 would manufacture a deep negative
proof from a shallow crawl. I had the frontier confused with a set minimum.

I have not started the contiguous-frontier design. Before I do, one question
below.

## Verification

```text
code head    ef2fb18

                              failed   passed   skipped
main control (origin/main)         1     5320         4
this branch                        1     5382         4
```

Same single pre-existing failure both sides
(`test_dv_settings::test_all_frontend_editable_settings_keys_are_in_model`).
**+62 passing, zero net new failures.** Host/container md5 parity asserted for
the run.

## The question for this round

**Where should the coverage-run record live, and who writes it?**

Your `coverage_runs` shape implies the crawler emits a proof per arm per run,
which means `_crawl_pages()` grows a second output alongside the claim ledger. I
can see two ways and I do not want to pick wrongly a third time in this area:

1. **The crawler emits the frontier** — it is the only thing that knows page
   order, contiguity and parser health, so nothing else can honestly attest them.
   But it puts proof-construction inside the component whose partiality started
   all of this.
2. **The crawler emits raw traversal facts** (ordered page/URL/parse-status
   records) and a separate consumer derives the frontier from them, so the proof
   is reconstructible and inspectable after the fact rather than asserted once.

I lean 2, on the same principle that made the claim ledger inert: record what was
observed, derive conclusions separately. But it is more machinery, and it stores
per-page traversal data whose volume I have not estimated.
