# Media-type authority: the persisted representation

Design package, 2026-08-31. Branch `design/media-type-authority`, cut from
`origin/agent/hybrid-sweep-rebased` (`d04ab63`).

**This document specifies a model. It changes no production code.** The only
executable artefact beside it is `tests/test_media_type_authority_properties.py`,
whose properties are either characterisation tests of today's behaviour or
`xfail(strict=True)` statements of what the model must hold.

Round 5's verdict is accepted: the persisted authority representation is wrong
and should be replaced rather than patched further. Sections 9 and 10 say where
that verdict is stronger than the evidence supports, and where the reviewer's
sketch did not survive contact with this codebase. Everything numbered below was
executed against the branch head; the probes lived under `%TEMP%` and are not
committed.

---

## 0. What was verified, and what it cost to find out

| # | Claim | Verified? | How |
|---|---|---|---|
| V1 | The resolver has 4 authority levels; persistence stores 1 boolean; the adapter therefore upgrades TITLE→DETAIL and downgrades IDENTITY→DETAIL | **YES** | executed `resolve_media_type` → persist → `cached_verdict_evidence` for each level. ROUTE and DETAIL round-trip; TITLE and IDENTITY do not |
| V2 | That loss currently produces a wrong verdict | **NO — 0 cases** | relabelling the reloaded verdict DETAIL→TITLE changed the answer on **0 of 138** reachable rescan inputs |
| V3 | Why V2 holds: the evidence lattice is degenerate | **YES** | `Authority.IDENTITY` appears in `tests/` only — **no production code emits it**. Above ROUTE, every producer emits `TV` and nothing emits `MOVIE`, so no two levels above ROUTE can ever disagree |
| V4 | The listing path can persist a non-provisional MOVIE | **NO — 0 cases** | exhaustive over the real source table; reachable persisted pairs are only `('ambiguous',True)`, `('tv',True)`, `('tv',False)`, and `('movie',True)` after a rescan |
| V5 | A rescan is idempotent today | **YES, 0 violations** | over the 27-state closure of current-format rows, and over the 77-state closure that also contains the pre-#93 legacy corpus shape |
| V6 | **NEW, LIVE: the listing writer ignores the conflict it is recording** | **YES** | `resolve_listing_media_type` never reads `post_info['category_conflict']`. It writes `media_type='movie'` on the same row it stamps `category_conflict=True`, so the stored verdict disagrees with the effective conflict-aware cache interpretation of that row |
| V7 | **NEW, LIVE: two readers of one row disagree after `mark_scan_category_conflict`** | **YES, 3 of 12** | `results.py:704` serves the **raw blob** (`media_type`, and `_effective_category` reads `category` directly), while the matcher goes through `cached_media_type`. After an out-of-band conflict mark the API says `'movie'`/`'tv'` and the matcher says `'ambiguous'` |
| V8 | The provenance the new model needs is already persisted | **YES** | `media_type_because` is written by four producers and columns exist on `hdencode_candidates`; **nothing anywhere reads it back** |
| V9 | A per-row writer-entitlement mechanism already exists in this codebase | **YES** | `DatabaseManager._PROTECTED_FIELDS`, `_COUPLED_FIELD_GROUPS` and the `detail_authority_fields` column on `hdencode_candidates` |

**The honest headline.** The representation is wrong; the specific harm named in
the verdict is *latent*, not live. What is live is a different pair of defects
(V6, V7) of exactly the class the verdict predicts — a derived summary stored
beside the claims it summarises, and writers that record an observation without
re-deriving what depends on it. Those were found by writing this document, not
by another patch, which is the argument for doing the design.

V6/V7 are reported, not fixed, in this lane.

**Wording correction (round 6 review).** V6 previously read "every reader of
that row then answers `'ambiguous'`". That is not true of *every* reader, and
V7 is the counter-example: raw `results.py` serves the **stored** value. The
accurate statement is that the stored verdict disagrees with the **effective
conflict-aware cache interpretation** of the same row. Substance unchanged —
the defect, and its size, are the same.

---

## 1. The canonical schema

Three ideas carry the whole design:

1. **Authority is the name of the slot, not a value in it.** An observation
   stored in the `title` slot *is* TITLE authority. It cannot be written with
   the wrong authority, cannot be re-read at a different one, and cannot be
   promoted by a later write. P1 becomes true by construction rather than by
   assertion — which is the point of replacing the representation instead of
   testing it harder.
2. **A verdict is a cache, physically separated from the observations, and
   carries a digest of the set it came from.** A stale verdict is detectable and
   is discarded, not served (V7).
3. **`resolve` accepts an `ObservationSet` and nothing else.** It is not
   possible to hand it a verdict, so R4-94-2's whole class is a type error
   rather than a rule someone must remember (§7).

```
Attestation = 'unknown' | 'clean' | 'conflict'
Provenance  = 'native'  | 'legacy'
MediaClaim  = 'tv' | 'movie'          # never 'ambiguous': that is an OUTPUT

Observation:                          # one slot's content; authority is implied
    claim:       MediaClaim
    source:      str                  # 'listing-title', 'detail-filename', ...
    observed_at: str                  # ISO-8601 UTC
    provenance:  Provenance

RouteEvidence:                        # the ROUTE slot is richer than the others
    category:    str                  # '4k' | 'remux' | 'tv' | ''  -- SEE NOTE
    claims:      tuple[MediaClaim, ...]   # EVERY listing claim seen, deduped,
                                          # in first-seen order.  P7 lives here.
    attestation: Attestation          # DERIVED from `claims` + whether a
                                      # conflict-aware crawl observed the row
    sources:     tuple[str, ...]      # the listing that made each claim
    observed_at: str
    provenance:  Provenance

ObservationSet:                       # the ONLY input to the resolver
    route:    RouteEvidence | None
    title:    Observation   | None
    detail:   Observation   | None
    identity: Observation   | None

Verdict:                              # DERIVED. A cache. Never an input.
    media_type:         'tv' | 'movie' | 'ambiguous'
    deciding_authority: 'route' | 'title' | 'detail' | 'identity' | 'none'
    reasons:            tuple[str, ...]   # structured, not display strings
    observations_digest: str              # sha256 over the canonical
                                          # serialisation of the ObservationSet
    grammar_version:    str               # release_grammar.GRAMMAR_VERSION

MediaTypeState:                       # what a row persists
    schema_version:  int              # 1
    observations:    ObservationSet
    verdict:         Verdict | None   # cache; may always be dropped
    legacy_verdict:  ('tv'|'movie') | None   # §3 only; never on a native row
    provenance:      Provenance
```

**NOTE on `category`.** `category` stays exactly what it is today: independent
crawl/routing/display metadata (`'4k' | 'remux' | 'tv'`), used by the UI facet
and by `get_scan_category`'s media-kind authorisation. It is *not* a media-type
observation. The media-type observation derived from the route is
`RouteEvidence.claims`, which is a different thing: the source table couples
them (`4k`/`remux` ⇒ `type: 'movie'`, `tv` ⇒ `type: 'tv'`, verified at
`scanner_service.py:760-778`) but they answer different questions and the
coupling is a property of today's source list, not of the model.

### `provisional`, restated

`provisional` stops being stored. It is `verdict.deciding_authority in ('route',
'none')`, **or** any deciding observation has `provenance == 'legacy'`
(§3, rule L3). It is served as a compatibility view (§4).

### Why `reasons` is structured

Today `media_type_because` holds strings like `"cached-title=tv"` and
`"listing-route=movie (overruled)"`. Four producers write it; **no consumer
reads it** (V8). Structured reasons — `(slot, source, claim, role)` — are the
same information in a shape a reader can act on, and the storage cost is already
being paid.

---

## 2. Writer / owner table

The rule the table encodes: **an operation may write only the observation slots
it actually observed, must carry the rest unchanged and uninterpreted, and may
never write a slot from a value it derived.**

| Operation | `route` | `title` | `detail` | `identity` | `verdict` | `category` |
|---|---|---|---|---|---|---|
| conflict-aware listing crawl (`_process_posts`) | **write** (`claims` append, `attestation`, `sources`) | **write** (listing title) | write *only* from the fresh detail filename it fetched | carry | recompute | **write** |
| manual detail rescan (`POST /scan/rescan-item`) | carry | carry | **write** (fresh filename only) | carry | recompute | carry |
| identity resolver (IMDb / unique Plex match) | carry | carry | carry | **write** | recompute | carry |
| legacy attestation backfill (`attest_scan_categories`) | **write** `attestation` only | carry | carry | carry | recompute | carry |
| conflict marker (`mark_scan_category_conflict`) | **write** `claims` + `attestation` only | carry | carry | carry | **recompute** ← today it does not | carry |
| resolver | — | — | — | — | **write** (sole writer) | — |
| cache re-match (`rematch_cache`) | carry | carry | carry | carry | carry | carry |
| serializer (`_media_item_to_dict`) | serialize | serialize | serialize | serialize | serialize | serialize |

Three entries are worth defending.

**`mark_scan_category_conflict` must recompute.** Today it writes
`category_conflict = True` into the blob and stops, so the row's stored
`media_type` is stale until some unrelated operation touches the row — which is
V7, measured at 3 of 12 reachable rows. Under this table it appends the claim,
re-derives `attestation`, and re-derives the verdict; the digest check in §7
makes forgetting detectable rather than silent.

**The listing crawl "recomputes" rather than "writes" the verdict.** V6 is
exactly the failure of writing a verdict that does not account for an
observation the same operation is recording. Recompute-from-the-set is the only
formulation that cannot do that.

**The serializer's contract is negative, and it is the one part of today's code
that already gets this right.** It must not turn absent into false, must not
turn a verdict into evidence, and must not infer provenance.
`scanner.py:386-390` skips `category_attested` when it is `None` for precisely
this reason, and that special case is what P6 pins.

### The structural cause of this whole bug class

`upsert_background_cache` does `data = excluded.data` — **whole-blob
replacement**. Every "carry" in the table above is therefore hand-written at
each call site, and each omission is a silent field deletion. R4-94-3 (C4) is
one such omission (`category_attested` dropped by rescan); R4-94-1 is another
(carried evidence dropped). A `MediaTypeState` that is read, mutated in named
slots, and written back as a whole makes "carry" the default and "write"
the exception, which inverts the failure mode.

The mechanism for this already exists in the repo, on the *other* store:
`_PROTECTED_FIELDS`, `_COUPLED_FIELD_GROUPS` and `detail_authority_fields` give
`hdencode_candidates` per-row, per-field write entitlement (V9).
**The recommendation is to port that mechanism, not invent one.**

---

## 3. The one-way legacy adapter

`read_legacy(row) -> MediaTypeState` with `provenance='legacy'`. It is a
**reader**. It is never persisted, and no code path writes its output back.

Mapping (each line justified by the code it replaces):

| Pre-schema field | Becomes | Rule |
|---|---|---|
| `category` ∈ `{tv,4k,remux}` | `route.claims = (tv\|movie,)` | as `cached_type_evidence` does today |
| `category_conflict = True` | `route.attestation = 'conflict'` | the *claims* are unrecoverable from a bare bool — see L4 |
| `category_attested` present/True | `route.attestation = 'clean'` | key **presence** is the tri-state carrier |
| `category_attested` absent | `route.attestation = 'unknown'` | absence is not False |
| `title` | `title` slot, via `title_type_evidence` | TV or nothing; a silent title is not MOVIE |
| `season is not None` | `title` slot, claim TV | as today |
| `is_tv = True` **and no stored `media_type`** | `detail` slot, source `legacy-is-tv` | genuine recovered observation |
| `is_tv = True` **and stored `media_type`** | **discarded** | it is a shadow of the verdict (R4-94-2) |
| `media_type` ∈ `{tv,movie}` | `legacy_verdict` | a verdict, **never** an observation |
| `media_type = 'ambiguous'` | `legacy_verdict = None` | the record of having decided nothing |
| `media_type_provisional` | **discarded** | it is a lossy encoding of an authority the slots now carry exactly |

Four rules, three of which were forced by measuring the adapter against
`cached_media_type` over the reachable row space:

- **L1 (one-way).** Reading never writes. Verified today: `cached_media_type`,
  `cached_type_evidence` and `cached_verdict_evidence` do not mutate the row
  they are given.
- **L2 (idempotent).** `read_legacy(read_legacy(row))` is not a thing that can
  be written — the output is a different type from the input. Repeated reads of
  the same row gave identical states in 144 of 144 cases.
- **L3 (never invent authority).** A legacy-provenance deciding observation
  keeps `provisional = True` regardless of which slot decided. **The first
  version of the adapter got this wrong**: derived from slot alone, it cleared
  `provisional` on 35 of 72 legacy rows — inventing exactly the authority
  `cached_media_type` deliberately refuses to claim. With L3 in place: 0 rows
  gain authority.
- **L4 (a conflict is a record, so the floor cannot apply).** `legacy_verdict`
  is used only when the recovered observation set is **empty**. It is a floor,
  below ROUTE, never a competitor. The first version applied it whenever
  derivation yielded nothing, which re-admitted a suppressed route on a
  conflicted row — reintroducing the R4-94-3 defect on 4 rows. With
  `attestation='conflict'` counting as a non-empty record, that cannot happen.

**Cost of L3, stated plainly.** Every pre-schema row reads `provisional=True`
until it is next observed, including rows that today read `provisional=False`.
That is a downgrade on the whole existing corpus. It is acceptable **today**
because no consumer of `background_scan_cache` gates on that flag: the only gate
is `hdencode_action_service.py:505`, which reads `hdencode_candidates`, a
different store fed by the RSS/hydration path. If a scan-cache gate is ever
added, L3 must be revisited first. This is the single largest behavioural
consequence of the design and it is deliberate.

---

## 4. Compatibility views, and when each disappears

| Field | Becomes | Definition as a view |
|---|---|---|
| `is_tv` | derived | `verdict.media_type == 'tv'` |
| `media_type_provisional` | derived | `deciding_authority in ('route','none')` or any deciding observation is legacy |
| `category_conflict` | derived | `route.attestation == 'conflict'` |
| `category_attested` | derived (tri-state) | `route.attestation` → `True`/`False`/`None` |
| `media_type` | derived | `verdict.media_type` |
| `category` | **stays** | independent crawl/routing/display metadata; not a view |

**Phase A — additive.** `MediaTypeState` is written alongside the five existing
fields. Every existing field keeps its current writer and its current meaning.
Readers are untouched. The property suite runs against the new state; the
existing suite is unchanged. Nothing can regress because nothing reads the new
state yet.

**Phase B — reads move.** The five fields become computed views over
`MediaTypeState`, emitted by the serializer with byte-identical shapes. The
legacy adapter (§3) supplies state for pre-schema rows. Writers still write both.
Exit criterion: the property suite passes with strict markers removed, **and**
a differential run over the live corpus shows the view values equal the stored
values on every row except the L3 `provisional` downgrade, which is expected and
counted.

**Phase C — writes move.** Producers write `MediaTypeState` only. The five
fields become serializer output. `cached_verdict_evidence`,
`conflict_suppresses_stored_verdict` and `stored_media_type` are deleted — all
three exist only to reconstruct authority the representation failed to store.
This is the phase that removes the R4-94-x patch chain.

**Phase D — fields disappear from storage.** The four view fields stop being
persisted. `is_tv` survives longest: 65 files reference the token, most of them
locals, and `frontend/src/lib/api/types.ts:934` declares it on the wire. It
should stay a serialized view indefinitely rather than being chased out of the
API; the goal is that nothing *decides* from it, not that the word vanishes.

---

## 5. Property tests P1–P8

`tests/test_media_type_authority_properties.py`. Status of each **against
today's code**, which is the point of writing them now:

Result at `d04ab63`: **7 passed, 1 skipped, 4 xfailed**, and every xfail fails
for the reason its marker names, not on an import or an unrelated assert.

| | Property | Today | Marker |
|---|---|---|---|
| P1 | exact authority round trip | **FAILS by construction** | `xfail(strict=True)`, plus a companion that asserts the loss so the defect is pinned |
| P2 | no self-authorization | **FAILS** (V6) | `xfail(strict=True)` |
| P2b | a row cannot clear its own provisional flag | passes | executable — this is the R4-94-2 regression pin |
| P3 | idempotent rescan, no new evidence | passes | executable |
| P3b | idempotence on the canonical evidence state | unrepresentable | `skip`, with the reason |
| P4 | commutativity of independent observations | passes | executable |
| P5 | writer entitlement / noninterference | passes for the three route facts | executable — what R4-94-3/4 bought |
| P6 | unknown is representable | passes, via one special case | executable; pins `scanner.py:386-390` |
| P7 | conflict is evidence, not a veto bit | **FAILS** — the claims are not stored | `xfail(strict=True)` |
| P8 | legacy conversion one-way | passes | executable |
| P8b | legacy conversion never gains authority | **FAILS** | `xfail(strict=True)` |

### Every passing assertion was shown to fail

A property nobody has seen fail proves nothing, so each was run against a
mutation of the production line it guards, by line number. **7 mutants, 0
survivors**, each killed with the intended message:

| Mutant | Line | Kills |
|---|---|---|
| `cached_verdict_evidence` returns TITLE instead of DETAIL | `scanner_service.py:2324` | P1 companion |
| revert R4-94-2: drop the `legacy_row` guard on cached `is_tv` | `scanner_service.py:2249` | P2b |
| widen R4-94-3: a conflict suppresses **every** stored verdict | `scanner_service.py:2296` | P3 |
| `resolve_media_type` first-wins instead of highest-authority-wins | `release_grammar.py:253` | P4 |
| `bool()` the tri-state attestation | `api/routes/scanner.py:122` | P5 |
| drop the UNKNOWN-attestation branch from the serializer | `api/routes/scanner.py:388` | P6 |
| `cached_media_type` caches its reconstruction back onto the row | `scanner_service.py:2366` | P8 |

**Two findings from doing this, both recorded in the test file itself:**

1. **P3 does not catch R4-94-2, and the first draft claimed it did.** Reverting
   the `legacy_row` guard SURVIVES idempotence, because R4-94-2's defect is a
   one-time promotion (`provisional` cleared once, then stable), not an
   oscillation. P2b exists because of that surviving mutant.
2. **P3 and P8 are toothless without the legacy corpus shape in the input
   space.** On a current-format row `is_tv` is a shadow of the verdict, so
   re-admitting it changes nothing; the feedback loop only appears on a row
   where `is_tv` is independent — which is every row written before #93, i.e.
   most of the deployed cache. `_legacy_rows()` is what makes those two
   properties able to fail at all.

`hypothesis` is not in the runtime image (`tests/test_queue_liveness_model.py`
records the same finding), so these are exhaustive deterministic enumerations
over a small finite space, not randomised properties. That is a feature here:
"exhaustive" is literal, and a reported failure is already minimal — both
mutation reports above name a single row.

`xfail(strict=True)` is chosen deliberately: when the model lands and a property
starts holding, the suite goes **red** until the marker is removed. A skip would
go quietly green and nobody would notice the marker was stale.

---

## 6. Migration decision: **no migration**

Convert on read; never write back.

Reasons, in order of weight:

1. **A migration would have to invent the thing that is missing.** The reason
   the corpus needs an adapter is that authority was never stored. A batch job
   would have to guess it for ~4,073 rows, in one shot, with no way to review
   the guesses. That is the R4-94-x mistake at corpus scale.
2. **The adapter is measurably faithful.** With L3 and L4, the only difference
   from today's `cached_media_type` is the intended `provisional` downgrade.
3. **Rows heal naturally.** `upsert_background_cache` sets
   `derived_state='current'` on every re-scrape, and the crawl re-observes the
   corpus continuously. A row converted on read today is written as native state
   the next time anything genuinely observes it.
4. **No consumer is blocked by the downgrade** (§3, L3 cost).

What this costs: for as long as legacy rows exist, two code paths exist. That is
the standard price of convert-on-read and it is bounded by re-observation, not
by a migration window.

**One thing a migration *would* be needed for, and is deliberately not being
done:** the conflicting *claims* behind an existing `category_conflict = True`
are gone. No adapter can recover them. Those rows convert to
`attestation='conflict'` with `claims` empty — honest, and strictly no worse
than today. They gain real claims the next time a conflict-aware crawl sees
them.

---

## 7. Proof sketch: a derived verdict can never re-enter as evidence

The single rule: **`resolve` takes an `ObservationSet`. `Verdict` is not a
member of `ObservationSet`. There is no function from `Verdict` to
`Observation`.**

That is the whole argument, and it is a typing argument rather than a discipline
argument. Spelled out:

1. `ObservationSet` has exactly four slots, each typed `Observation | None`
   (`route` being `RouteEvidence | None`).
2. `Observation.claim` is `MediaClaim` = `tv | movie`. `Verdict.media_type` is
   `tv | movie | ambiguous`. The types are not the same and there is no
   conversion, so a verdict cannot be widened into an observation even by
   accident.
3. `Verdict` carries `deciding_authority`, which no `Observation` has, and
   `Observation` carries `source` and `observed_at`, which a verdict cannot
   supply — a verdict was not observed anywhere at any time. Constructing an
   `Observation` from a `Verdict` requires inventing both fields, which is a
   visible act at a review-able line, not an omission.
4. `MediaTypeState.verdict` is reachable only through the field named `verdict`.
   `resolve(state.observations)` is the only call shape; `resolve(state)` does
   not type-check.
5. Therefore R4-94-2's defect — "the system reading its own answer back in" —
   has no expressible form. `cached_verdict_evidence` exists today *only*
   because `media_type` + `media_type_provisional` is the sole surviving record
   of a decision. Once the observations are stored, that function has no job and
   is deleted in Phase C.

**Corollary, which is the fix for V7.** `Verdict.observations_digest` pins the
verdict to the exact set it came from. Any reader recomputes the digest; on a
mismatch the verdict is discarded and re-derived. A writer that mutates
observations and forgets to recompute therefore produces a *detectably* stale
cache instead of a silently wrong answer, and the two readers in V7 cannot
diverge because the raw-blob reader would see the mismatch too.

This does not make forgetting impossible. It makes forgetting loud, which is the
strongest honest claim available: the digest is checked at read time, so a
forgotten recompute costs a re-derivation, not a wrong verdict.

---

## 8. Blast radius

Token counts across `backend/`, `frontend/`, `tests/` at `d04ab63`. Raw token
counts, so locals named `is_tv` are included; the classification below separates
them.

| Field | backend | frontend | tests | distinct files |
|---|---|---|---|---|
| `media_type` | 253 | 43 | 473 | 98 |
| `is_tv` | 131 | 1 | 351 | 65 |
| `media_type_provisional` | 29 | 0 | 52 | 20 |
| `category_conflict` | 22 | 0 | 39 | 13 |
| `category_attested` | 18 | 0 | 32 | 11 |
| `category` | 188 | 124 | 302 | — |

38 test files reference `is_tv`; 16 reference the three narrower fields.

### The consumers that actually matter

**Decides an action (must be correct at every phase):**

- `scanner_service.py:1775-1793` — `_match_against_plex` selects the Plex
  library from `web_item['media_type']`, with an explicit tri-state refusal for
  anything outside `{tv, movie}`. This is the real consumer and it already reads
  the verdict, not the boolean.
- `scanner_service.py:1795, 1855, 1877` — three further branches on
  `web_item['is_tv']`, which `web_item_facts` derives as
  `media_type == 'tv'`. Already a view; Phase B is a no-op for them.
- `hdencode_action_service.py:505` — the auto-grab gate, on
  `media_type_provisional`. **Different store** (`hdencode_candidates`). The
  scan cache does not reach it. This is the one place the flag authorises
  anything, and §3's L3 must not be allowed to reach it.
- `database.py:5301, 5310` — `get_scan_category` authorises the media kind for a
  destructive Keep-best, from `category_attested` and `category_conflict`. It
  reads the blob directly and fails closed. Phase B must preserve the
  absent-vs-False distinction exactly or this silently starts answering.

**Serves a wrong answer today (V7), and is the reason for the digest:**

- `api/routes/results.py:704` — serves the raw blob. `_effective_category`
  (line 35) and `_bookmark_key_for_item` (line 58) read `category` and
  `media_type` straight out of it, bypassing `cached_media_type`.

**Reconstructs authority, and is deleted in Phase C:**

- `scanner_service.py:2169` `stored_media_type`, `:2186` `cached_type_evidence`,
  `:2253` `conflict_suppresses_stored_verdict`, `:2299` `cached_verdict_evidence`,
  `:2330` `cached_media_type`, `:2369` `resolve_rescan_media_type`.
  `api/routes/scanner.py:37` `rescan_classification`.

**Not affected, despite matching the grep:**

- `rename/service.py:439, 1014`, `rename/conflicts.py:348`,
  `filename_utils.py:167, 189`, `rt_scraper.py:219`, `matching.py:156, 351`,
  `database.py:4555` — these are locals, function parameters, or the
  `plex_cache` table's own `is_tv` column. None reads the scan-cache field.

---

## 9. What this costs, and what it does not fix

**Does not fix:**

- **Nothing above ROUTE ever says MOVIE.** `title_type_evidence` returns TV or
  nothing; the detail filename asserts TV or nothing. So `AMBIGUOUS` above ROUTE
  is unreachable, and a film mis-shelved on a TV page has no signal that can
  correct it beyond the route. The new representation stores this faithfully —
  it does not create evidence that is not being gathered.
- **`Authority.IDENTITY` is still unused.** The slot exists in both the current
  enum and the proposed schema; no producer fills it. An IMDb type, or a unique
  Plex match, would be the first real IDENTITY signal and is the change that
  would make V1's loss live. This design makes that safe to add; it does not add
  it.
- **V6 and V7.** They are reported here and fixed by the model, but the model is
  three phases away. If they matter sooner they need their own small change —
  and that change is another entry in the patch chain, which is a decision for
  the reviewer, not for this lane.
- **`category` semantics.** `source_category` holds the source *name* on every
  live row while the crawl category lives in the blob (round 11). Untouched.

**Costs:**

- Roughly 3 review rounds of work across 4 phases, not one change.
- Phases A and B require both representations to be written at once, so a write
  path exists that can disagree with itself. The digest (§7) is what makes that
  disagreement detectable; without it, Phase B is more dangerous than the
  current state.
- 16 test files assert on the three narrower fields directly. Phase B changes
  those from stored values to computed views; assertions that construct a row
  with `media_type_provisional=False` and expect it honoured will change meaning
  under L3.
- The whole existing corpus reads `provisional=True` until re-observed (§3).
- A `MediaTypeState` blob is roughly 4–6× the bytes of the five fields.
  `background_scan_cache` holds ~4,073 rows; at a few hundred bytes each this is
  under a megabyte and is not a constraint.

---

## 10. Where the reviewer's sketch did not survive contact

Four places. Each is a change to the sketch, with the evidence.

**1. "The adapter necessarily upgrades TITLE→DETAIL and downgrades
IDENTITY→DETAIL" — true, but it produces no wrong answer today.** 0 of 138
reachable inputs change verdict when the reloaded authority is relabelled (V2),
because no producer emits IDENTITY and nothing above ROUTE emits MOVIE (V3, V4).
The case for the redesign is that the model is unreasonable-about — four
consecutive fixes each found the next defect — and that any new IDENTITY
producer activates the loss silently. It is not "the system is answering wrong
today". Presenting it as the latter would not survive a reviewer running the
same enumeration.

**2. "Conflict is evidence, not a veto bit" — right, and the sketch understates
it.** The reviewer's reason is that a summary bit goes stale when the claims
move. Measured, the *inverse* is what is actually happening: the claims are
frozen in a bool and the **verdict** goes stale when the bit moves (V7, 3 of 12
rows), because `mark_scan_category_conflict` writes the bit and nothing
re-derives. So `RouteEvidence.claims` is necessary but not sufficient — the
`observations_digest` in §7 is the part that actually fixes the observed defect.

**3. "A manual detail rescan may update fresh detail evidence only" — correct,
and already true.** R4-94-1 through R4-94-4 got the rescan route there. The
route that still violates the rule is the **listing crawl** (V6): it writes a
verdict that ignores the conflict it is simultaneously recording. The
writer/owner table in §2 is therefore aimed at a different operation than the
sketch expects, and `_process_posts` is the higher-risk site.

**4. "The legacy adapter preserves uncertainty rather than inventing
authority" — this needs an explicit clamp, which the sketch does not have.**
Derived naively from slots, the adapter *gained* authority on 35 of 72 legacy
rows, because a legacy row's recovered season/title/is_tv evidence genuinely
sits above ROUTE while `cached_media_type` deliberately refuses to claim that.
Rule L3 (§3) is the clamp; without it the adapter is a silent corpus-wide
promotion, which is worse than the representation it replaces.

One further deviation, in the sketch's favour: it proposes a writer/owner table
as new work. **This codebase already has one** — `_PROTECTED_FIELDS`,
`_COUPLED_FIELD_GROUPS`, `detail_authority_fields` on `hdencode_candidates`
(V9). The recommendation is to port it to `background_scan_cache` rather than
design a second mechanism that can drift from the first.

---

## Appendix: reproducing the measurements

Each figure above came from importing the production functions directly
(`backend.release_grammar`, `backend.scanner_service`,
`backend.api.routes.results`) and enumerating, never from restating their logic
in a probe. The reachable state space is built by:

1. running `resolve_listing_media_type` over the **real** source table
   (`scanner_service.py:760-778`, which couples `category` to `type`);
2. persisting exactly what the writers persist
   (`media_type`, `media_type_provisional`, `is_tv = media_type == 'tv'`);
3. closing under `resolve_rescan_media_type` and under
   `mark_scan_category_conflict`;
4. pinning the detail observation to one bit, because
   `detail_scraper.py:285-287` makes `season is not None` imply `is_tv`.

Step 4 matters: without it the enumeration includes `(season=3, is_tv=False)`
rows that no scraper can produce, and the violation counts inflate from 0 to 220
on inputs that do not exist. The first two runs of this analysis reported those
inflated numbers.
