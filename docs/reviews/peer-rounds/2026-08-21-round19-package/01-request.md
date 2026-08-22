# Round 19 — M18-1 through M18-4, and the identity the policy was blocked on

All four round-18 findings are addressed. Two of them turned out to have a live
instance in the shipped configuration rather than a hypothetical one, and one of
those instances is in data the deployed container has already written.

Three open questions at the end, two of them new and both about choices I made
where a different answer is defensible.

---

## M18-1 — three incompatible arm identities

**Closed.** `backend/arms.py` is now the single definition. It imports nothing
from the crawler, the database or the evaluator, so all three depend on it
without a cycle.

You flagged the DDLBase remux pair as the example. It is not an example — it is
shipped:

```text
DDLBase Remux 4K      /cat/movie-remux-2160p    ddlbase:remux
DDLBase Remux 1080p   /cat/movie-remux-1080p    ddlbase:remux
```

Both `movie`/`remux`. So beyond the join being impossible, `listing_claim_seen`
was keyed `(url, listing_type, CATEGORY)` and whichever feed listed a release
**second** had its claim discarded as a repeat of the first. The ledger exists
precisely to keep what each arm said before releases age off the listing.

What changed, by layer:

```text
traversal    arm_key_from_descriptor(source)         backend.arms
ledger       the key the producer stamped            backend.arms, via the claim
contracts    (arm_key, parser_version)               was report.source
```

The contract change matters on its own: keyed on `source`, a contract
established for one HDEncode endpoint would have marked 4K, Remux and TV Packs
authoritative together — three separate empirical claims minted from one piece
of evidence — and it would have survived a parser rewrite that changed what
listing order *means*. That is gate item 7, and it has its own tests in both
directions: the contracted arm IS authoritative, the sibling and the
parser-mismatched arm are not.

`ArmRegistry` **refuses to build** when two descriptors resolve to one key,
rather than merging them. A silent merge is invisible: the crawl runs, the
ledger fills, and two feeds quietly share one identity.

### The part that is not just code — the rows already on disk

The deployed container has written its ledger under two-part keys. As of
2026-08-22 16:11Z, 266 claims across 255 releases:

```text
hdencode:tv     105
hdencode:4k      93
hdencode:remux   68
```

The counts grow every cycle; the shape does not. Three keys, each mapping to
exactly one feed, is what the migration acts on.

If those are left alone while new rows arrive in the three-part shape, the
ledger carries **both** keys for the same feed: the same release twice, and a
coverage summary reporting six arms where there are three.

`migrate_listing_claim_arm_keys()` moves them — once per process, idempotent,
before the first new-shape write. The rules it follows:

- **It moves a row only when the answer is KNOWN.** Each of the three live keys
  resolves to exactly one feed. `ddlbase:remux` resolves to two, so those rows
  stay legacy and the key is **logged**. A legacy row is still a true record of
  a sighting; what it must never do is acquire a precision it never had. Logged
  rather than skipped quietly, because a silent skip is indistinguishable from a
  migration that had nothing to do.

- **It takes the COMPLETE registry, never the sources selected for one scan.**
  This is the trap I nearly walked into. `_build_sources()` returns the feeds
  selected for the current scan, gated by source type and per-category flags.
  With only the 2160p remux feed selected, `ddlbase:remux` looks *unambiguous* —
  and resolving it would attribute rows that may have come from the 1080p feed.
  A migration built from a partial view is worse than no migration. There is a
  test asserting exactly that difference between a partial and the full
  registry.

- **Where both shapes exist, it MERGES rather than clobbers.** Reachable after a
  deploy, a rollback and a redeploy. Earliest `first_seen_at`, latest
  `last_seen_at`, summed `sightings`, and `posted_date_changed` set if *either*
  row saw a change. That last one is not bookkeeping: the flag disqualifies a
  release from anchoring a frontier, so losing it would quietly restore an
  anchor that had been ruled out. Alias rows are rekeyed with the claim —
  revocation enumerates aliases, and a variant it cannot find is a download row
  that keeps its media kind after the release has been contradicted.

`KNOWN_ARMS` is hand-written, so a test asserts it against what
`_build_sources` actually emits, **in both directions**. A feed added there and
not here would become unmigratable, silently; a feed here that nothing produces
would let an ambiguous legacy key look resolvable.

---

## M18-2 — the duplicate set was global to the crawl

**Closed.** `_cov_seen_canonical` was created outside the source loop.

I want to be precise about why this was a false-proof path rather than a
counting bug, because that is your framing and it is right. `duplicate_in_run`
means "proves no new depth in THIS arm", and a first sighting always does prove
depth. The evaluator **skips** duplicates — so an out-of-order date that should
have triggered an inversion refusal was silently *removed*, and the arm returned
a frontier instead.

Now `Dict[arm_key, Set[canonical_url]]`. Both regressions you asked for are at
the producer:

- two arms sharing a release, each arm's first sighting eligible
- the inversion shape, asserting the later arm **refuses** rather than dropping
  the observation. Before the fix that arm returned a quiet "no corroborated
  anchor"; after it, `listing order inversion at page 1 position 3`.

The positive control is in the same class: a cosmetic raw variant of a release
already seen in the *same* arm is still flagged, so per-arm scoping did not
reopen M17-1.

---

## M18-3 — a page spoke for posts it had not read

**Closed, both halves.**

*Producer.* `_cov_arm.pages.append(_cov_page)` ran before a single post was
enumerated. An exception partway through left a page whose `request_outcome`
said `ok` and whose `parser_state` said `recognised`, carrying only the
sightings read before the failure. The page is now sealed only after complete
enumeration. A failure mid-enumeration seals the **partial** page marked
unusable — the sightings are a true record of what was read, so they are kept,
but nothing can mistake the page for a complete observation. The page variable
is also reset per iteration, or a later exception could have sealed the previous
page's object under this page's number.

*Evaluator.* Positions were checked for uniqueness and then **sorted**, which
accepted `[1, 3]` — a page that lost a sighting — and `[2, 1]`, a page whose
emitted order contradicted its claimed order. Sorting hid both. Now requires
exactly `1..N` in emitted order, and the walk no longer re-sorts, since the
assertion immediately above it establishes that emitted order *is* position
order.

---

## M18-4 — the evidence could change under a proof

**Closed.** `CoverageEvidenceSnapshot` is frozen and **copies**. You were
explicit that a `MappingProxyType` over the caller's dictionary is insufficient
because the caller still holds the original; `capture()` does
`MappingProxyType(dict(dates or {}))` and `frozenset(unstable or ())`.

I also made the two-reads problem structural rather than argued. `_anchor()`
now returns `(when, raw)` from a single read, so `frontier_date` and
`frontier_date_raw` cannot describe two different observations even in
principle. Passing a snapshot *and* a second `unstable` set raises rather than
picking one.

Anti-vacuity control included: "immune to mutation" must not mean "ignores
input", so a test asserts the snapshot still reflects the values at capture.

---

## Gate items

```text
1  unify arm identity everywhere            DONE
2  scope duplicates per arm, + regression   DONE
3  seal pages after enumeration, 1..N       DONE
4  immutable evidence snapshot              DONE
5  versioned required-arm policy            NOT DONE -- needs your ruling
6  persist evidence and proofs              NOT DONE -- needs your ruling
7  contract does not authorise siblings     DONE
8  rerun main/branch like-for-like          DONE, see 03-evidence.md
9  deploy dark, inspect persisted runs      BLOCKED on 6
```

5 and 6 were blocked on the identity, which is why 1 came first. Nothing in this
round can mint attestation: `ORDERING_CONTRACTS` is still empty and still
enforced in code, no caller sets `attest_coverage=True`, and nothing writes
`category_attested`.

---

## Three questions

### 1. The required-arm policy — still open from round 18

Unchanged from what I asked last round, but the join it needs now exists.
`covers_release()` takes the required keys explicitly, which closes the
existential bug, but nothing derives that set from a target's claimed type.
Which arms can contradict a given release is a domain judgement and a wrong
answer produces a confidently wrong negative.

### 2. Persistence — still open, and one thing in your schema

Your schema is detailed enough to build from and M18-4 removed the concern I
raised about it. The remaining question is your ruling that a proof must never
be persisted as a timeless permission bit: I read that as meaning the stored
row records *what was proven at a moment against sealed evidence*, and any later
read must re-derive whether that still authorises anything. Confirm before I
build it, because getting this backwards is the whole failure mode.

### 3. NEW — is refusing at startup the right call for a key collision?

`endpoint_slug` is the last path segment of the base URL. Two feeds that differ
only by query suffix — `?tag=movies` versus `?tag=tv` on the same path — would
produce one key, and `ArmRegistry` raises `ArmKeyCollision` rather than merging
them.

I chose refuse-loudly because a merge is exactly the defect this module exists
to remove, and because no shipped feed currently collides. But it means a
configuration change could stop the scanner from starting. The alternative is
folding a normalised suffix into the endpoint, which never refuses but produces
uglier keys and would change the keys already emitted in round 17.

I do not think this is close, but it is a startup-availability tradeoff on
Jesse's running system, so I would rather you disagreed with it now than after
it fires.

### 4. NEW — should the migration run automatically at all?

It currently runs on the first claim-recording of a process. The alternative is
a manual one-shot that Jesse runs deliberately, leaving the deploy inert.

Arguments for automatic: it is idempotent, it merges rather than clobbers, it
declines to guess, and leaving it manual means the first deploy produces the
double-keyed ledger described above until someone remembers.

Argument against: it writes to live rows during a dark deployment, and "dark"
has so far meant *reads nothing, writes nothing that grants anything*. This
writes something that grants nothing — but it does write.

I went with automatic. Tell me if that crosses the line you drew.
