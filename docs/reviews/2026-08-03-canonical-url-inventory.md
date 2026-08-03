# Canonical-URL identity inventory + measured corpus (seven-item gate, item 2)

**Date:** 2026-08-03 · **Author:** Claude (4-agent enumeration + corpus sweep, key claims re-verified by hand) · **Reviewer:** ChatGPT (round 9) · **Arbiter:** Jesse
**Tree:** `agent/hybrid-sweep-implementation` @ `b1825f1` · **Corpus:** consistent
backup-API snapshot of the production DB taken 2026-08-03 ~02:40 UTC
(31,731,712 bytes, 34 tables). All controls run before any zero was believed.

## 1. The canonicalisers — three implementations, two output forms

| Function | Form | Behaviour |
|---|---|---|
| `hdencode_feed_parser.canonicalize_post_url` (:86-96) | **A** | https-only (raises otherwise), forces bare `hdencode.org` (drops www), collapses `//`, **appends** trailing slash, drops query+fragment |
| `url_identity.canonicalize_listing_url` (:12-34) | **B** | lowercases scheme/host, **strips** trailing slash, drops query+fragment; **keeps www**, keeps `//` |
| `hdencode_shadow.canonical_url` (:11-15) | **B** | duplicated implementation of the above, not shared code |

Executed proof: `HTTPS://WWW.HDEncode.org/Foo/?utm=1#x` → Form B
`https://www.hdencode.org/Foo` vs Form A `https://hdencode.org/Foo/`. Three
spellings of one post can therefore coexist across stores (raw, A, B).
A fourth ad-hoc canonicaliser lives in SQL: `RTRIM(…,'/')` at
`database.py:1720-1728`, which bridges ONLY trailing slash.

## 2. The corpus, measured (snapshot; SQL in the agent transcripts, spot-checked)

* **Form A (trailing slash), ~26k URLs:** `hdencode_candidates` 2969 (PK; guid
  mirror intact 2969/2969), `candidate_feeds` 4568/2969 distinct,
  `hydration_queue` 2888, `candidate_details` 2033 — all 100% exact FK joins;
  plus the raw-href stores whose raw form happens to equal A: `scanned_urls`
  300, `background_scan_cache` 2918, `downloads` 417, `dismissed_items` 1018,
  `download_queue_items` 213, `pipeline_verdicts` 203, `scraped_link_map` 1599.
* **Form B (no slash):** `hdencode_shadow_misses` **112/112**,
  `listing_policy_exclusions` **149/149**.
* **Cross-form exact join is DEAD:** shadow_misses ∩ candidates exact = **0**;
  with `|| '/'` appended = **111 of 112**. Exclusions ∩ candidates exact =
  **0**; slash-appended = 114 of 149 (35 never entered RSS candidates — a
  population observation, not a form bug). The 1 residual miss
  (`pallichattambi-2026-…`) is genuinely absent from candidates under both
  forms but present WITH slash in `background_scan_cache`/`dismissed_items`.
* **No other variance exists:** 0 http rows anywhere, 0 host-case variants,
  0 intra-table form-pair duplicates, 0 whitespace taint. The trailing-slash
  schism is the ONLY identity divergence in the data — but it splits the
  Phase A instruments (shadow ledger, Form B) from the acquisition population
  (candidates, Form A).
* **Query strings are identity-bearing for feeds only:** `hdencode_feed_state`
  has 3 pairs identical except `?tag=movies` vs `?tag=tv-shows`. Both Form-B
  canonicalisers drop the query — safe only while never applied to feed URLs.

## 3. Consumers that define Phase A, ranked

1. `compare_shadow` (`hdencode_shadow.py:80-113`) — re-canonicalises BOTH
   inputs itself, so the schism converges there; fail-closed
   `disjoint_identity_sets` guard commemorates the 0-of-100 incident. Sound.
2. **Sweep listing-ledger frontier** (`sweep/session.py:174-209`, PK
   `(source_key, canonical_url)`) — **canonicaliser UNBOUND**:
   `CANONICALIZER_VERSION='1'` names no function; `record_observations` stores
   the caller's string verbatim; **no production caller exists at `b1825f1`**
   (tests only). Whichever form gets wired decides whether any future
   RSS↔sweep join works at all. This is the single highest-leverage decision
   item 2 surfaces.
3. Incremental skip (`scanner_service.py:885-891`) — raw-vs-raw by documented
   design (`url_identity.py:19-25`); mismatch cost is re-scrape, not a miss.
4. Candidate upsert — dual identity on one table: PK `canonical_url` + UNIQUE
   `guid` (`database.py:890-893`); a site permalink-form change re-keys the URL
   but not the guid → IntegrityError → **whole feed-ingest transaction rolls
   back** (fail-loud, acquisitions stop).
5. `exact_url_downloaded` (`database.py:1720-1728`) — RTRIM bridge on
   `source_url` but EXACT join on `downloads.url`, a column holding **two
   identity kinds** (listing path writes post URLs; RSS path writes hoster
   links per `hdencode_action_service.py:314-317`) → both directions fall back
   to title keys; failure mode is duplicate grab (spend), not data loss.
6. `download_queue_items.canonical_url` is canonical **in name only**
   (`download_queue.py:303` prefers the raw `url`, no canonicaliser; the
   UNIQUE active-item index dedupes raw strings).

## 4. P0 FOUND AND HAND-VERIFIED — #191's RSS exclusion record is dead code

`hdencode_rss_service.py:333` builds the exclusion record from **`entry.link`**,
but `ParsedFeedEntry` (`hdencode_feed_parser.py:38-53`) has **no `link` field**
(the field is `canonical_url`). The first excluded entry raises
`AttributeError`, the `except Exception` at :339 downgrades it to a warning,
and `record_policy_exclusions` never runs.
`tests/test_rss_full_disc_symmetry.py` passes only because its `FakeEntry`
(:54-56) defines the `.link` the real dataclass lacks — the same
wrong-test-double shape as the wrong-listing-reader finding.

Consequences: entries ARE still excluded from ingest (`_split_full_disc` is
sound — correctness holds), but the A7/#191 acceptance criterion "the exclusion
is **counted and observable**" fails in any deployed build, and the advertised
"second writer (RSS) must not break the invariant" at `database.py:3703-3706`
never executes. Caught pre-merge; production (main) does not contain this code.

**Prescribed fix (round 9, not applied tonight by overnight ground rule):**
first make the test fail — use the real `ParsedFeedEntry` in the test, watch
`AttributeError` surface — then change `entry.link` → `entry.canonical_url`
(+ `entry.title` is valid) and assert the durable rows exist with
`REASON_RSS_FULL_DISC`. Note the write boundary canonicalises to Form B
(`database.py:3703-3707`), so RSS- and listing-written exclusion rows will
share one form — correct by construction once the writer actually runs.

## 5. What item 2 requires before Phase A (proposed closure criteria)

1. **One shared canonicaliser module** with an explicit version constant; the
   two Form-B duplicates delegate to it; the Form A/B split either unified or
   documented as two named identities with an explicit bridge at every join.
2. **Bind the sweep ledger to it** — `record_observations` canonicalises (or
   validates) rather than trusting callers; `CANONICALIZER_VERSION` names the
   function it describes.
3. **Fix the #191 recording P0** with the real-dataclass test.
4. **Declare the Phase A population identity in the thresholds doc:** the
   acquisition population = Form A candidate keys; the miss ledger = Form B;
   every instrument that joins them must state its bridge (today only
   `compare_shadow`'s internal re-canonicalisation and `miss_resolution.py`'s
   same-producer consistency make this sound).
5. Corpus re-measured after 1–3 land; the cross-form join counts above are the
   baseline to beat (0 exact matches must become 100% under the bridge).

## 6. Round-9 additions to the closure criteria (2026-08-03)

§5 alone does not close item 2. Additionally required: (1) committed
executable measurement + control queries with snapshot provenance/checksum
(no sensitive production data); (2) machine-readable outputs with fixed
denominators, explicitly carrying the 1 residual miss and the 35
out-of-RSS-population exclusions; (3) "100% under the bridge" defined per
join and per population; (4) consumer-boundary contract tests for every
identity bridge Phase A relies on, including the sweep frontier once bound;
(5) a migration/backfill or compatibility policy for already-persisted A/B
keys when the shared canonicaliser version changes. Frontier identity:
**Form A**, as a source-specific versioned HDEncode post identity with raw
URL retained; feed identity stays a DISTINCT function because feed query
parameters are identity-bearing. §5 criterion 3 (the #191 fix) is DONE
(7681a87, CI actions/runs/30811406913; assertions sharpened 1c1fab3).

## 7. Migration / compatibility policy for persisted keys (§6.5)

Persisted identities are NEVER rewritten in place. Policy, effective with
`hdencode-post-v1` / `listing-v1`:

1. **A version bump is a schema event.** Changing any identity function
   requires: a new version string, a dual-read bridge (old-form and new-form
   lookups both consulted) shipped in the SAME commit, and a one-shot,
   counted re-key migration whose before/after row counts are recorded as
   evidence. Rows that cannot be re-keyed are listed, not dropped.
2. **Never during a window.** Any identity version change invalidates a
   running qualification window by definition (it changes the acquisition
   population); the migration must land before a window opens or after it is
   graded, never inside one.
3. **Existing corpus is already version-stamped or accounted:** sweep-ledger
   rows carry `canonicalizer_version` per row; `hdencode_candidates` keys are
   uniformly Form A (measured: 2969/2969 trailing-slash, 0 exceptions);
   the two Form-B ledgers are uniformly slash-stripped (112/112, 149/149).
   Reproduce with `scripts/canonical_url_corpus.py` — committed output at
   `docs/reviews/evidence/2026-08-03-canonical-url-corpus.json`
   (snapshot sha256 `69fb7c2cbbcfd904…`, controls embedded).
4. **"100% under the bridge", defined per join (§6.3):**
   shadow_misses→candidates: denominator 112, bridged 111, the 1 unmatched
   row is listed in the JSON and separately proven a genuine
   never-in-candidates absence, not a form mismatch. exclusions→candidates:
   denominator 149, bridged 114, the 35 unmatched are the out-of-RSS-
   population set, listed in full. A future "pass" must cite these same
   denominators or explain the population change.
