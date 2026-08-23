# ScanHound — Round 20 plan, REVISION 2

Revised against your 12-item gate. Still a plan review: M19-1 and M19-2 remain
unwritten deliberately.

Everything below that is new is **measured**, not designed. Where measurement
contradicted my previous plan, I say so explicitly rather than quietly
correcting it — including one claim I made to you that was simply false.

---

## 0. Four corrections to what I told you last round

**0.1 — I told you "nothing writes `category_attested`". That is FALSE.**
`background_scanner.py:575` writes it, via `database.py:4244/4279`. It has been
writing all along. The mitigating detail, which I did not know either: `attested`
is classified *before* claim membership is consulted, so quarantine can only ever
touch the non-attested remainder. But the statement as I made it was wrong and it
appeared in a section headed "Not covered", which is where a reviewer is least
likely to challenge it.

**0.2 — I said three consumers lack an arm filter. There are SIX.** And a blanket
"add an arm filter" instruction would have been actively harmful on one of them.
See §5.

**0.3 — My backup protocol was not merely unsound, it demonstrably loses data.**
You ruled `.db + -wal + -shm` wrong. It is now proven: a naive `.db` copy taken
183 ms after the good ones was missing **27 individually-named committed rows**
across six tables. It passed `integrity_check`, `quick_check` and
`foreign_key_check`. See §7.

**0.4 — The running container is no longer the Round-14 writer.** I deployed
unrelated work onto `main` on 2026-08-22 and did not notice that `main` has no
ledger code at all. `listing_claims` has been **frozen at 266 rows since
2026-08-22T15:50:43Z**. No live harm — the ledger feeds attestation, which is not
enabled — and it removes a hazard, because the migration input now has no
concurrent writer. But it means "the running container is Round-14 code", stated
in my last package, stopped being true partway through.

---

## 1. Context that changed underneath this plan

Unrelated to the migration, but it changes the throughput picture you were given:

```text
2026-08-16   20 reveals    2026-08-21   20 reveals
2026-08-17   20            2026-08-22   20
2026-08-18   20            2026-08-23   49   <-- adapter switched
```

Five consecutive complete days at exactly 20, never 21. The scraper was switched
from plain Selenium to undetected-chromedriver and the same day did 49 in four
hours. The "~20/day site quota" was never a site rule; it was metering of a
client that announced `navigator.webdriver=true` and `--enable-automation`. The
ceiling appeared on 2026-07-24, the day after the switch *to* plain Selenium. It
was self-inflicted for a month.

This does not change the migration. It does mean the ledger will grow faster than
the 266 rows once attestation code is deployed again.

---

## 2. `ArmRevision` — gate items 1 and 3

Adopted as you specified. The durable identity is the revision, not the name:

```text
ArmId          arm.hdencode.4k-2160p          stable, declared, OPAQUE
ArmRevision    (arm_id, request_definition_version, parser_version)
```

- `listing_claims` PK → `(canonical_url, arm_id, request_definition_version, parser_version)`
- `listing_claim_aliases` PK → the same, plus `raw_url`
- traversal `Arm`, proofs, and `ORDERING_CONTRACTS` all carry the full revision
- `covers_release()` compares the required *active revision* to the proof
  revision, never two equal `arm_id` strings
- the required-arm policy names stable `arm_id`s and resolves each to its active
  eligible revision through the versioned registry

**Opaque means opaque.** Beyond choosing dots over colons as a tripwire, I will
add a static guard test that fails on `split(":")`, colon-counting, or any
source/category reconstruction from an arm identity anywhere in `backend/`.

**Registry lifetime.** One central registry service, not independent
reconstructions. The blast-radius review found a second real construction site I
would have missed — `ui/controllers/scanner_controller.py:414` — alongside
`api/main.py`. Each run captures an immutable registry snapshot before its first
fetch; a config reload atomically replaces the registry for the *next* run and
never mutates `ArmSpec` objects under an active traversal.

---

## 3. The digest — gate item 2

Your ruling accepted: digest, not hand-incremented.

```text
request_definition_version = "request-v1:" + sha256(canonical_json(request_definition))
```

Canonical structure covers everything that can change the selected or ordered
stream: method, normalized scheme/host/port, path template, query parameters and
suffix placement, pagination template **including page 1 versus page N**, content-
selecting headers/cookie mode, and the normalizer schema version.

**The canonical JSON is stored beside the digest.** A digest without its preimage
is not auditable, and a future normalizer change would otherwise leave no way to
explain why a contract became invalid.

Nothing is hashed from a function name, a `repr`, dict insertion order, or a
callable's identity. Pagination is a declared enum — and the investigation found
this matters more than I assumed: **the pagination branch has four forms, not
two.** The `adithd` branch silently drops the query suffix on page N. HDEncode
takes the `else` branch so its verdict is unaffected, but a two-branch summary
would hide that asymmetry the moment adithd is back-attributed.

---

## 4. Historical manifest — gate item 4

Built from the deployed writer, not inferred from the current registry.

**Anchor: commit `ef2fb1883423`**, not an image id. The image
`sha256:8256fc8b5b32` (local tag `scanhound:rollback-20260822-121500`) is the only
one on the host containing the writer, and it has **no pushed manifest** — one
`docker image prune` destroys the evidence. Its `backend/` tree is byte-identical
(CRLF-normalised) to `ef2fb188`, which is pushed to
`origin/fix/round12-attestation-authority`. The git anchor survives independently
and is what the manifest cites.

Whole-tree comparison, 94 `.py` files: **91 identical**, 3 differ, 2 branch-only.
The entire request stack — transport, coordinator, `url_identity`, network,
scrapers, source definitions — is untouched, so there is no room for a hidden URL
rewrite.

| legacy key | deployed request (page 1 / page N) | today | verdict |
|---|---|---|---|
| `hdencode:4k` | `/quality/2160p/?tag=movies` / `/quality/2160p/page/N/?tag=movies` | identical | **attributable** |
| `hdencode:remux` | `/quality/remux/?tag=movies` / `/quality/remux/page/N/?tag=movies` | identical | **attributable** |
| `hdencode:tv` | `/tag/tv-packs/` / `/tag/tv-packs/page/N/` | identical | **attributable** |

`_build_sources`, the pagination block and `_select_posts` each hash identical.
Host `hdencode.org` is proven from the data — all 266 `raw_url` netlocs — not only
from config. Resolving the *deployed* descriptors through the registry yields full
`ArmSpec` equality with **unresolved = []**.

**Quarantine set for these 266 rows is empty.**

Two honesty markers the manifest will carry:

- **`parser_version = "select_posts/1"` is RECONSTRUCTED, not recorded.**
  `_COV_PARSER_VERSION` does not exist in the deployed image; it is branch-only.
  The value is justified by byte-identity of the parser and by nothing else.
- **What wrote the 266 rows is strong circumstantial evidence, not causation.**
  Hand-written scratch scripts dated 8/20–8/21 exist in the working tree and were
  never opened. The image match is compelling; it is not proof.

---

## 5. Consumer rules — gate item 10

Six sites, not three. **No consumer has an arm filter today** — `WHERE arm_key = ?`
appears only inside the migration itself.

| consumer | direction | rule |
|---|---|---|
| `consume_cross_crawl_conflicts` detection `:4845` | narrows only | include quarantined rows, via an asserted keyword-only `include_unattributable=True` so a refactor cannot add a filter |
| **same fn, alias expansion `:4863`** | narrows | **NEVER FILTER.** Filtering here is *fail-open*: raw variants would keep a contradicted media kind. A blanket arm-filter instruction breaks exactly this one |
| `media_kind_coverage_summary` `:4914` | widening-flavoured | separate and label: `claimed_attributable` / `claimed_quarantined` as distinct keys, plus `quarantined_arms`. Never summed |
| date backfill **fill** branch `:4767` | **widens** — feeds `covers_release` | exclude quarantined rows, in the **SQL WHERE at `:4723`** |
| date backfill **flag** branch `:4773` | **narrows** | keep unfiltered, as a second pass |
| `get_listing_claims:4784`, `listing_claim_summary:4794` | no callers | annotate before a dashboard merges them invisibly |

The date backfill is the subtle one: it is a single function whose two branches
point in opposite directions, so the rule is **per branch, not per function**.
And there is a trap — `LIMIT 2000` applies to the JOINed rowset before any Python
filter, so the exclusion must be in SQL or quarantined rows starve legitimate ones.

Measured today: backfill 0 fills / 0 flags / 11 skipped; conflicts 0; coverage
`unknown_claimed` 178 of 4357, `unknown_claimed_quarantined` would be 0.

---

## 6. Quarantine and the latching problem — gate items 11 and 4

Your two-state model, adopted:

```text
migration_epoch_complete   every input row classified migrated-or-quarantined,
                           invariants passed, audit record committed
proof_eligible(row)        exact registered ArmRevision, not quarantined
```

`ddlbase:remux` stays permanently quarantined and visible without blocking
unrelated HDEncode proofs. A separate `legacy_quarantined_rows > 0` metric, so
permanent quarantine is never disguised as incomplete execution.

**Openly unresolved:** quarantine has no design yet — no table, no column, no key.
Whether it keys on `arm_key` or `(arm_key, migration_run_id)` decides all six
consumer rules above, so it is the next thing I will specify, not something I will
discover during implementation.

---

## 7. Backup protocol — gate item 8

Your ruling upheld and now proven rather than asserted.

**Four captures within 183 ms, naive copy taken last.** Full primary-key set
difference across all 36 tables:

```text
VACUUM INTO      0.15 s   row-for-row identical to live
con.backup()     0.11 s   row-for-row identical to live
cp .db + -wal    0.04 s   identical
cp .db alone     0.03 s   MISSING 27 named committed rows
```

The 27 are individually named across `download_results`, `downloads`,
`download_queue_attempts`, `scraped_link_map`, `download_package_links`,
`background_scan_cache` and one shadow cycle. **Ordering control:** rerun with the
order reversed — deficit again 27, same direction, zero reverse. Skew would have
flipped the sign; it is structural. Rollback is non-uniform: 11 minutes on one
table, **2 h 52 min** on another. The bad copy passed every integrity check.

**Isolation, previously untested:** with a writer holding an open `BEGIN IMMEDIATE`
of 50 uncommitted rows, both good techniques returned committed=100,
committed_wal=50, uncommitted=0.

**Procedure:**

1. `VACUUM INTO` from a `mode=ro` connection to **container-local** storage —
   0.15 s, 59 MB. Never to `/data`: the 9p path takes 16–21 s, holds the read lock
   throughout, and inflated the live WAL from 4.1 to 13.3 MB. Copy to durable
   storage afterwards, holding no lock.
2. `integrity_check` + `foreign_key_check` on the result.
3. **Monotonic sandwich** — anchor live immediately before and after capture and
   require `before ≤ backup ≤ after`. This replaces a single-sided anchor, which
   races and needs an arbitrary tolerance. Both good techniques PASS; `.db`-only
   FAILs.
4. sha256, copy to durable storage, re-verify there.

**Two hazards worth recording.** `cp .db + -wal` **mutates itself on first read**
(62,152,704 → 62,210,048 bytes, `-wal` deleted) — the artifact archived is not the
one verified. And **never use `immutable=1` in verification**: on pristine live
bytes it returned the stale pre-WAL state with `integrity_check ok` while a normal
open of the same bytes saw four more rows.

WAL exposure re-measured at 673 live frames = **2.63 MB**; the file size overstates
it 4.8×. Quote as measured at time T, never as a property of the file.

**Also to fix:** `DatabaseManager.checkpoint()` discards the
`(busy, log, checkpointed)` tuple and returns `True` unconditionally — it cannot
distinguish a truncated WAL from an untouched one. Sound test:
`busy == 0 and log == 0 and os.path.getsize(wal) == 0`.

---

## 8. Maintenance entry point — gate item 7

New `scripts/migrate_listing_claim_arms.py`, dry-run by default, `--apply` to
commit. Modelled on the existing `scripts/migrate_challenge_episode.py`.

**May import:** stdlib; `backend.arms` (dataclasses and typing only);
`backend.runtime_lock`; a new stdlib-only `backend/schema.py`.
**Must NOT import:** `backend.database`, `backend.config` (import-time
`makedirs`/`shutil.move`/db copy), `backend.app_service`, `backend.api.*`.
**DB path:** explicit `--db`, defaulting to the literal `/dbvol/crawler.db`.

**Why `DatabaseManager()` is disqualified — five side effects, not one:**

1. `open_revocation_session()` fsyncs a SESSION_OPEN record with no `atexit`. The
   next app start sees an unclosed session, sets `_authority_disabled` process-wide,
   and `get_release_identity()` then returns `media_kind=None` and `season=None`
   for **every release**. A total authority outage caused by a run I would have
   described as read-only.
2. `init_db`'s integrity check is wired to `_quarantine_corrupt_db()`, which
   `os.rename`s the 62 MB production database and rebuilds an empty one.
3. Unconditional data INSERTs, `PRAGMA user_version=9`, and a checkpoint.
4. A `DROP TABLE listing_claims` guarded on an `order_key` column — verified
   absent live, but present in the code path.
5. Import-time filesystem mutation in `backend.config`.

**Sequence:** acquire `RuntimeWriterLock` (already deployed; `AppService.startup()`
takes it *before* `DatabaseManager()` and does not catch the failure, so the app
cannot start underneath) → backup per §7 → open live RW with
`busy_timeout=30000` → `integrity_check`, abort on non-ok, **never quarantine** →
`schema.apply_schema()` → `seed_listing_claim_aliases()` → freeze the §4 manifest →
`BEGIN IMMEDIATE` → migrate → invariants **inside** the transaction → commit or
roll back as a unit → checkpoint, release, print JSON.

**Two-phase, not a sentinel.** `plan_migration(read_only_conn)` returns an
immutable plan plus an input fingerprint; `apply_migration(conn, plan)` commits and
returns an output fingerprint. Executed and committed on the copy to measure
postconditions; on live, the input fingerprint is recomputed and compared before
applying. That makes the dry run genuinely read-only rather than a write that
relies on rollback behaving.

**Shared DDL** via a new stdlib-only `backend/schema.py` exposing
`apply_schema(cursor)` — all CREATEs, indexes, and an `_ensure_columns()` that
collapses the 36 ALTERs currently copy-pasted across five sites — with no INSERT,
DROP, version stamp or commit. Precedent exists at `database.py:304`.

**Three extraction blockers**, which is why this is a design and not a done
refactor: an `ALTER TABLE downloads ADD COLUMN jd_confirmed_name` *is the gate* for
the backfill beneath it, so moving it makes that backfill permanently unreachable;
the `download_results` PK rebuild is a migration, not schema; and the five ALTER
sites must collapse together or one gets dropped.

**Hard constraint: `SCHEMA_VERSION = 9` must not be bumped.** It is pinned at four
co-dependent sites and is not CI-only — bumping it flips the **live** RSS shadow
qualification to not-ready. The migration does not need a bump;
`listing_claim_aliases` is created by an ungated `CREATE TABLE IF NOT EXISTS`.

**Collision window is minutes, not zero.** The background scanner waits one
interval (floor 300 s) before its first cycle, but `POST /background/scan-now`
closes that instantly. Run with the app container stopped.

---

## 9. Sequencing — gate items 5 and 6

Your ruling on preflight, adopted. The direct two-part → final revision migration
holds **only if** preflight proves: no three-part keys exist; live keys and counts
match the frozen inventory; the manifest maps each key uniquely; and the database
has not changed between plan and apply. Any drift aborts. No compatibility path is
added mid-run.

The frozen ledger (§0.4) strengthens this: the input has had no writer for ten
hours, so the fingerprint is stable by construction rather than by luck.

**Idempotent and retry-safe, not merely "exactly once."** A stable migration ID
whose completed state commits atomically with the row changes, distinguishing: not
started / planned against fingerprint F / committed / quarantined-by-design /
unexpected partial, which refuses.

---

## 10. What is still unknown, and stays unknown until tested

Named rather than smoothed over:

- **The in-place restore has never been exercised.** Stop, move aside, place
  backup, start — untested end to end. Writing to `/dbvol` at all is unverified
  for space and for permissions as the `scanhound` uid. This is the largest
  untested step in the protocol.
- **No round-20 code has ever been executed.** No rehearsal against a copy, no
  suite run, no image built. Your rehearsal requirement is unmet.
- **Cross-container `flock` on the named volume is unproven**, and the entire
  app/maintenance interlock rests on it. A positive control — hold in one
  container, confirm `acquire()` raises in another — runs before step 2.
- **Quarantine has no design** (§6).
- **Whether `ddlbase`/`adithd` are enabled live is unread.** "Quarantine set is
  empty" is true of the existing 266 rows only, not of the set after redeploy.
- **`apply_schema()`'s first-run delta against live was never enumerated** beyond
  confirming `listing_claim_aliases` is absent.
- **`scan_history` is completely empty** — there is no first-party scan log, so
  coverage claims are not supported by this data.

## 11. Questions

1. Does the frozen ledger change your preflight requirements, given the input can
   no longer drift?
2. Is `parser_version` reconstructed-from-byte-identity acceptable provenance, or
   should rows written by an image with no parser-version constant be quarantined
   on principle?
3. The date backfill's two branches point in opposite directions within one
   function. Split it, or gate per branch as proposed?
