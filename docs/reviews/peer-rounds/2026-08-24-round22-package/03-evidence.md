# Round 22 — evidence

Every number re-measured for this package on 2026-08-24. The live database was
never written to; measurements against real data use a `VACUUM INTO` copy.

---

## 1. The deployed ledger, re-measured at package time

```
rows: 266, user_version: 9
max last_seen_at: 2026-08-22T15:50:43.046582+00:00
```

Unchanged since the round-21 package, and still frozen — so no measurement here
moved underneath the work.

## 2. The new schema, against the real 266 rows

```
deployed snapshot: 266 rows

after shape migration:
  unattributed  arm_id=None rdv=None pv=None legacy=hdencode:4k      93
  unattributed  arm_id=None rdv=None pv=None legacy=hdencode:remux   68
  unattributed  arm_id=None rdv=None pv=None legacy=hdencode:tv     105
content identical to the deployed rows: True

CHECK constraint:
  refused: attributed with no arm_id
  refused: unattributed carrying an arm_id
  refused: unattributed with no legacy key
  refused: duplicate unattributed row for one release

attribution: attributed=266 merged=0 quarantined=0
  attributed  arm.hdencode.4k-2160p   93
  attributed  arm.hdencode.remux      68
  attributed  arm.hdencode.tv-packs  105

non-arm_id values that reached the arm_id column: 0
  legacy-shaped  -> unattributed arm_id=None legacy=hdencode:tv
  unregistered   -> unattributed arm_id=None legacy=unregistered:request-v1:deadbeef
  declared-one   -> attributed   arm_id=arm.hdencode.tv-packs

alias rows belonging to UNATTRIBUTED claims: 2
```

"content identical" compares the full nine-column projection as a set, not a
count.

## 3. The CHECK constraint is real

The exact schema now in force:

```sql
CREATE TABLE listing_claims (
    claim_id INTEGER PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    attribution_state TEXT NOT NULL,
    arm_id TEXT,
    request_definition_version TEXT,
    parser_version TEXT,
    legacy_arm_key TEXT,
    ...
    CHECK (
      (attribution_state = 'attributed'
         AND arm_id IS NOT NULL
         AND request_definition_version IS NOT NULL
         AND parser_version IS NOT NULL)
      OR
      (attribution_state = 'unattributed'
         AND arm_id IS NULL
         AND request_definition_version IS NULL
         AND parser_version IS NULL
         AND legacy_arm_key IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uq_listing_claims_revision
  ON listing_claims(canonical_url, arm_id,
                    request_definition_version, parser_version)
  WHERE attribution_state = 'attributed';
CREATE UNIQUE INDEX uq_listing_claims_legacy
  ON listing_claims(canonical_url, legacy_arm_key)
  WHERE attribution_state = 'unattributed';
```

The surrogate key is forced, not stylistic, and it is your own R21-2 reasoning
applied to the shape that replaces the one you withdrew: a composite key
containing nullable columns gives no uniqueness in a rowid table, because NULLs
compare distinct. The partial indexes constrain the two populations separately.

## 4. `user_version` is still 9

`docs/feature-pack-review/qualification/scripts/05_shadow_evidence.py` BLOCKS on
`user_version != 9`, so a bump would flip the live RSS shadow qualification to
not-ready for a change that adds tables.

## 5. Mutation results — do the new tests kill the old behaviour?

Each fix was verified by restoring the defect and confirming the tests fail.

### R21-5 — content preservation

Blanked `posted_date_raw` during the rebuild, a **count-preserving** corruption:

```
count 266 -> 266          <- the check this replaced passed exactly this
rows differing:      255
CAUGHT: listing_claims shape migration is not content-preserving
        (rows only in old=255, only in new=255, count 266 -> 266); refusing
```

255 rather than 266 because 11 rows already had a NULL date and are unchanged —
the check is precise, not merely loud.

### R21-10a — raw aliases

Restored the writer to keeping only the claim's own `raw_url`:

```
FAILED test_BOTH_raw_variants_survive_as_aliases
FAILED test_every_alias_is_reachable_from_the_revocation_query
2 failed, 7 passed
```

The second is the one that matters: it runs the exact enumeration
`consume_cross_crawl_conflicts()` performs, so the defect is caught at the
CONSUMER rather than at the column.

### R21-13 — contract transfer

Restored the old lookup semantics — match on `arm_id` + parser, ignore the
request definition:

```
FAILED test_a_DIFFERENT_request_definition_is_NOT_authoritative
FAILED test_an_undeclared_feed_can_never_match_a_contract
2 failed, 3 passed
```

The positive control (`test_the_reviewed_request_definition_IS_authoritative`)
still passes under the mutant, so the two failures are discrimination rather
than a test that always fails.

### Earlier, carried forward

The posted-date recency mutant still kills exactly one test —
`test_the_LEGACY_date_wins_when_the_legacy_row_was_seen_later` — which is the
test added after the original fixture was found unable to distinguish the fix
from the bug.

## 6. The alias collision, with deliberately different histories

R21-11 said the plain `UPDATE` hits the alias key and rolls the transaction
back. Under the new schema aliases follow `claim_id`, and the collision is
merged rather than resolved by discarding one side:

```
legacy alias   raw-A  first=2026-07-01  last=2026-08-01  sightings=3
target alias   raw-A  first=2026-08-01  last=2026-08-25  sightings=5
                 ->   first=2026-07-01  last=2026-08-25  sightings=8
raw-only-legacy       first=2026-08-01  last=2026-08-01  sightings=1  (untouched)
```

The untouched row is the anti-vacuity control: a merge that rewrote everything
would satisfy the first assertion while corrupting the rest.

## 7. SQLite parse behaviour, verified rather than assumed

The alias merge is an upsert attached to an `INSERT ... SELECT`. On 3.40.1:

```
FAILS  no WHERE, subquery      near "DO": syntax error
OK     trailing WHERE on outer
OK     direct with WHERE
```

So the SELECT's WHERE clause is load-bearing syntax, not a filter that can be
simplified away.

## 8. Pagination

The crawler no longer contains the four branches; `test_the_crawler_keeps_no_
second_implementation` greps for `page/{page_num}` and fails if they return.
The builder is checked against **literal** expected URLs (8 vectors across the
three forms and pages 1/2/7/9), and `test_the_crawler_actually_requests_those_
urls` captures what a real crawl asks for:

```
page 1  https://hdencode.org/quality/2160p/?tag=movies
page 2  https://hdencode.org/quality/2160p/page/2/?tag=movies
```

`test_the_three_forms_are_genuinely_different` asserts the three forms produce
three distinct page-2 URLs, so the enum cannot be decorative.

## 9. Test suite

```
identity file:  134 tests, all passing
full suite:     11 failed, 5634 passed, 4 skipped in 909.52s (0:15:09)
```

The 11 are the same ones that fail at `origin/main`, baselined in the same
container in this session: 8 `test_clicknload_fallback_wiring.py`, 1
`test_dv_settings.py`, 2 `test_round20_auto_resume_log_once.py`. None is
related to this work.

Passing count went 5559 → 5592 → 5634 across the three rounds of this branch.
