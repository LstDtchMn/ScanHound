# R-4 / R-5 / R-6 — design proposal for round 10 (NOT implemented)

**Date:** 2026-08-03 · **Author:** Claude · **Status:** PROPOSED — per the
autonomous-session charter, mechanism choices that are design-shaped go to
the round rather than being self-approved overnight. Nothing below is built.

## R-4 — derived-state invalidation/versioning (gate item 4)

**Measured baseline:** no grammar/parser version constant exists anywhere;
the only version stamps in the tree are the sweep ledger's per-row
`canonicalizer_version` (bound 2026-08-03, `hdencode-post-v1`) and
`promotion_gate`'s `parser_version` *binding field* (a string compared, never
produced). Derived classifications (`hdencode_candidates.media_type` +
`media_type_provisional`, detail payloads, `background_scan_cache` items)
carry no record of which grammar derived them.

**Proposed mechanism (recommend A):**

* **A. Stamp + lazy re-derive.** Add `GRAMMAR_VERSION` to `release_grammar`
  (bump on ANY parsing-behaviour change — this session alone would have
  bumped it: R-3's deltas). Stamp it on every row that persists a derived
  classification, at the same write boundary that persists the value.
  Consumers treat version-mismatch exactly like `media_type_provisional`:
  not trustworthy, re-derive through the EXISTING hydration/classify path,
  which re-stamps. No migration pass, no thundering herd (re-derivation
  happens at the moment a consumer would have trusted a stale value).
  Qualification instruments count `stale_derived` rows explicitly — a
  version bump mid-corpus becomes visible, not silent.
* B. Bump-time migration sweep (re-derive everything eagerly). Rejected:
  couples a code deploy to a full re-scan, and a partial sweep leaves a
  mixed corpus with no marker for how far it got.
* C. Derive-on-read only, no persistence. Rejected: reintroduces the exact
  cached-items-lose-their-type failure `604be2e` closed.

**Round-10 questions:** (1) Is per-row stamping on candidates + cache +
detail payloads the right grain, or does the candidate row alone suffice
given details/cache re-derive through it? (2) Does `GRAMMAR_VERSION` fold in
`POST_IDENTITY_VERSION`, or stay independent (recommend independent — URL
identity and title grammar change for different reasons)? (3) What does the
Phase A grader do with `stale_derived` rows — exclude-and-count, or fail the
cycle?

## R-5 — consumer-boundary contract suite (gate item 5)

Replace structural assertions with end-to-end decision tests at each
consumer boundary: given one release observed via RSS and via listing, the
SAME decision object fields (normalised identity, eligibility, media type,
year/season interpretation, quality class, action class, rejection/ambiguity
reason) must agree — the plan rev 2 equivalence contract, executed as tests.
Skeleton exists in the parity fixtures; the work is enumerating the consumer
boundaries (candidate context, library selection, action admission, dedupe,
UI results row) and asserting the CONTRACT fields, not internals. Proposed
to build immediately after R-4's verdict, since stale-derived semantics
feed the contract's "trustworthy input" definition.

## R-6 — wire the promotion gate (gate item 6)

`backend/promotion_gate.py` is pure and imported only by its test (measured
2026-08-02, unchanged). The review named four call sites where production
decides without it: `api/routes/rss.py:283`, `hdencode_rss_service.py:108`
and `:173`, `hdencode_action_service.py:347`.

**Proposed wiring, fail-closed:** the two CAPABILITY boundaries are
(1) *RSS-primary discovery admission* (rss service poll/candidate path
acting as primary rather than shadow) and (2) *automatic side-effect
admission* (auto-grab/auto-rename action creation). At both: evaluate the
gate; anything except a full pass denies the capability and records the
blocking reasons. Today every input to the gate is absent (no Phase A
verdict, no bindings), so the wired gate denies everywhere — **byte-for-byte
no behaviour change in production now** (the capabilities are already off),
but the OFF state becomes enforced by evidence rather than by a config flag
someone could flip early. Tests: wiring tests that flipping the config flags
WITHOUT a gate pass still denies (the anti-"flip it early" property), plus
the existing pure-gate suite.

**Round-10 question:** confirm the two boundaries above are the reviewer's
intended two, or whether shadow-mode polling itself should also consult the
gate (we think not — shadow is the evidence collector; gating it would
starve the gate of its own inputs).
