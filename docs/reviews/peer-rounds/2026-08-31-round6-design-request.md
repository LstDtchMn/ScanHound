# Round 6 — the #94 design package, and a correction to your own verdict

**Branch:** `design/media-type-authority` @ `1e50214` (no PR; design only)
**Base:** `agent/hybrid-sweep-rebased` @ `d04ab63` (PR #94, draft)
**Previous:** Round 5 — *"The persisted media-type authority model is wrong.
Stop extending the R4-94-x patch chain and replace the authority-bearing
representation."* You asked for a design package before any implementation.
This is it.

**It changes no production code.** 575 lines of design document, 519 lines of
property tests that are either characterisations of today's behaviour or
`xfail(strict=True)` statements of what the replacement must hold.

#101's crash-consistency work is NOT in this round. It is unfinished and
explicitly still open.

---

# Read this first: your structural claim is right, and your framing overstates it

**V1 — confirmed by execution.** `resolve → persist → reload` run for all four
authority levels. ROUTE and DETAIL round-trip. **TITLE is upgraded to DETAIL
and IDENTITY is downgraded to DETAIL**, exactly as you said, at
`scanner_service.py:2323-2324` reconstructing four levels from one boolean.

**V2 — and this is the correction.** That loss produces **no wrong verdict
today.** Relabelling the reloaded authority DETAIL→TITLE changed the answer on
**0 of 138 reachable rescan inputs.**

**V3 — the reason.** The evidence lattice is *degenerate*. `Authority.IDENTITY`
appears only in `tests/`; no production code emits it. Above ROUTE, every
producer emits TV and nothing emits MOVIE — so no two levels above ROUTE can
disagree. **V4:** the listing path cannot persist a non-provisional MOVIE at
all.

So the case for the redesign is **not** "the system is answering wrong". It is:

1. the model is *unreasonable-about* — four consecutive fixes each found the
   next defect; and
2. **any new IDENTITY producer activates the loss silently.** The defect is
   latent, and the thing that makes it latent is an accident of the current
   producer set, not a property of the design.

We are telling you this because presenting it as "answering wrong today" would
not survive you running the same enumeration. The narrower claim is the one we
can defend, and we think it still justifies the redesign — but that is your
call to confirm, and it is the first question below.

---

# Two LIVE defects found by doing the design work

Neither is fixed. Both were verified here before being written down.

## V6 — the listing writer does not apply the observation it is recording

`resolve_listing_media_type` **never reads `post_info['category_conflict']`**
(measured: 0 references in the function). The listing crawl writes
`media_type='movie'` onto the same row it stamps `category_conflict=True`, and
every reader of that row then answers `ambiguous`.

R4-94-3 fixed exactly this shape on the **rescan** route. The **listing** route
still has it.

## V7 — two readers of the same row disagree

`mark_scan_category_conflict` sets the bit in the blob and **nothing
re-derives the verdict**. `results.py:704` serves the **raw blob** —
`_effective_category` and `_bookmark_key_for_item` read `category` and
`media_type` straight out of it (measured: `results.py` contains 0 references
to `cached_media_type`) — while the matcher goes through `cached_media_type`.

**Measured: 3 of 12 reachable listing-produced rows have the API and the
matcher answering differently for the same row.**

This is the defect the proposed verdict-cache digest is designed to make
impossible, which is why it is reported here rather than patched — patching it
in the current model is another R4-94-5.

---

# The model

**Authority is the NAME OF THE SLOT, not a value in it.** An observation in the
`title` slot *is* title authority. It cannot be written at the wrong level,
re-read at a different one, or promoted by a later write. That makes your **P1
true by construction rather than by assertion** — which is the whole point of
replacing the representation instead of testing it harder.

**A verdict is a cache, physically separate from the observations**, carrying a
`sha256` digest of the observation set it was derived from. A stale verdict is
*detectable* and discarded rather than served. That is the fix for V7.

**`resolve()` accepts an `ObservationSet` and nothing else.**
`Observation.claim` is `tv|movie`; `Verdict.media_type` is `tv|movie|ambiguous`;
there is no conversion between them and **no function from `Verdict` to
`Observation`**. R4-94-2's entire class becomes a *type error* rather than a
rule someone has to remember.

The package also contains, as you asked: the writer/owner table, the one-way
legacy adapter with provenance marked `legacy`, the compatibility-view list and
its retirement phases, a migration decision with its reasoning, and the
consumer blast radius.

---

# What we want from you

**1. Does the narrowed claim still justify the redesign?** We can no longer say
the system answers wrong today, only that it cannot be reasoned about and that
one new producer activates the loss. If that is not enough to justify replacing
a persistence model, say so — we would rather hear it now than after the
implementation.

**2. V6 and V7 — fix now in the old model, or only in the new one?** V7 is
user-visible today (API and matcher disagree on 3 of 12 rows). Fixing it in the
current model is another local patch of exactly the kind you told us to stop
writing. Fixing it only in the new model leaves it live for however long the
redesign takes. We lean toward fixing V7 now and V6 in the new model, but that
is a judgement about your own instruction and we would rather you made it.

**3. Is "authority is the slot" the right primitive?** It is what makes P1
structural. If you see a case where an observation legitimately needs to change
authority level after being recorded, the whole shape is wrong and we should
know before implementing.

**4. Is the degeneracy worth removing on purpose?** Right now nothing emits
IDENTITY and nothing above ROUTE emits MOVIE. We could add a deliberate
producer of each to the test suite so the lattice stops being degenerate and
the property tests stop passing for the wrong reason. That is a bigger change
than it sounds.

---

# Evidence boundary

Every V-number above was executed here, on the owner's host, against the
current head. The enumeration counts (138 rescan inputs, 12 listing-produced
rows, 3 divergent) are author-reported; you can reproduce them from the
document, which names the enumeration.

The property tests in `tests/test_media_type_authority_properties.py` are
deliberately **not** passing assertions about a model that does not exist. They
are characterisations of today's behaviour plus `xfail(strict=True)` statements
of the target. `strict=True` matters: if the replacement accidentally satisfies
one early, the suite fails rather than silently going green.

R4-94-1 through R4-94-4 remain closed locally. No implementation of this design
has been written.

No merge, deployment, permission change, or enablement is authorized by this
review.
